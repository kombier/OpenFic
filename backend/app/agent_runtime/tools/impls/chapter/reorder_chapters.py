import json
from dataclasses import replace

from pydantic import BaseModel, Field
from sqlalchemy import text

from app.agent_runtime.revisions import (
    current_revision_id_from_state,
    images_by_id,
    record_agent_activity_for_change,
    record_chapter_diffs,
)
from app.agent_runtime.tools.base import AgentTool
from app.agent_runtime.tools.errors import ToolExecutionError
from app.agent_runtime.tools.impls._locks import keyed_locks
from app.agent_runtime.tools.impls.chapter.refs import (
    ChapterRef,
    VolumeRef,
    resolve_volume_from_list,
)
from app.agent_runtime.tools.registry import ToolRegistry
from app.storage.database import create_session
from app.storage.repos import chapter_repo, volume_repo
from app.storage.services import chapter_service
from app.storage.services.version_control_service import refresh_project_stats


class ReorderChaptersInput(BaseModel):
    volume_ref: VolumeRef = Field(description="目标卷")
    ordered_chapter_refs: list[ChapterRef] = Field(
        description="按新顺序排列的章节引用列表，必须包含该卷内全部章节，不允许遗漏或多余",
    )


@ToolRegistry.register
class ReorderChaptersTool(AgentTool):
    name: str = "reorder_chapters"
    description: str = "重排指定卷内的章节顺序，传入按新顺序排列的章节引用列表（须覆盖卷内全部章节）"
    access_level: str = "write"
    args_schema: type[BaseModel] = ReorderChaptersInput

    async def _execute(
        self,
        volume_ref: dict,
        ordered_chapter_refs: list[dict],
    ) -> str:
        revision_id = current_revision_id_from_state(self._state)
        if revision_id is None:
            raise ToolExecutionError("缺少当前 revision，无法执行章节重排")
        session = await create_session()
        try:
            # 读-校验-写整体持有章节锁：before 快照必须取自锁内最新状态，
            # 否则锁外读到的旧正文会进修订记录，回滚时覆盖其他工具并发
            # 提交的正文修改（维护者 review 指出的竞态）。
            async with keyed_locks([("chapters", self.project_id)]):
                volumes = await volume_repo.list_by_project(session, self.project_id)
                volume_ref_model = VolumeRef.model_validate(volume_ref)
                if volume_ref_model.type == "title":
                    # 同名卷拒绝静默取第一项（与章节标题歧义同策略）。
                    same_title = [
                        v for v in volumes if v.title == volume_ref_model.value
                    ]
                    if len(same_title) > 1:
                        dup_orders = "、".join(
                            str(v.order) for v in sorted(same_title, key=lambda v: v.order)
                        )
                        raise ToolExecutionError(
                            f"卷标题「{volume_ref_model.value}」不唯一"
                            f"（涉及序号 {dup_orders}），请改用 order 引用定位"
                        )
                volume = resolve_volume_from_list(volumes, volume_ref_model)
                volume_id = volume.id

                volume_chapters = await chapter_repo.list_by_volume(session, volume_id)
                ref_models = [ChapterRef.model_validate(ref) for ref in ordered_chapter_refs]

                # 基于锁内已加载的卷内章节做内存解析，避免逐章回查整行
                # （大卷会拉长其他章节工具的锁等待）。
                by_order = {chapter.order: chapter for chapter in volume_chapters}
                by_title: dict[str, list] = {}
                for chapter in volume_chapters:
                    by_title.setdefault(chapter.title, []).append(chapter)

                # 将每个传入的章节引用解析为卷内的具体章节
                resolved: list = []
                used_ids: set[str] = set()
                for ref_model in ref_models:
                    if ref_model.type == "order":
                        matched = by_order.get(int(ref_model.value))
                    else:
                        candidates = by_title.get(str(ref_model.value), [])
                        if len(candidates) > 1:
                            dup_orders = "、".join(
                                str(c.order) for c in sorted(candidates, key=lambda c: c.order)
                            )
                            raise ToolExecutionError(
                                f"章节标题「{ref_model.value}」在卷「{volume.title}」中不唯一"
                                f"（涉及序号 {dup_orders}），请改用 order 引用定位"
                            )
                        matched = candidates[0] if candidates else None
                    if matched is None:
                        raise ToolExecutionError(
                            f"未在卷「{volume.title}」中找到章节（{ref_model.type}={ref_model.value}）"
                        )
                    if matched.id in used_ids:
                        raise ToolExecutionError(
                            f"章节引用重复：{matched.title}（{ref_model.type}={ref_model.value}）"
                        )
                    used_ids.add(matched.id)
                    resolved.append(matched)

                existing_ids = {chapter.id for chapter in volume_chapters}
                if used_ids != existing_ids:
                    missing = [
                        chapter.title
                        for chapter in volume_chapters
                        if chapter.id not in used_ids
                    ]
                    extra = [
                        chapter.title
                        for chapter in resolved
                        if chapter.id not in existing_ids
                    ]
                    raise ToolExecutionError(
                        "章节引用必须与卷内现有章节一一对应："
                        f"缺少 {missing or '无'}；多余 {extra or '无'}"
                    )

                before = images_by_id(volume_chapters)
                ordered_ids = [chapter.id for chapter in resolved]
                # rollback 会使 ORM 对象 expire；title 等展示字段必须在 rollback 前取出纯值。
                volume_title = volume.title.strip()
                await session.rollback()

                # 写前校准：章节锁只约束 Agent 工具，REST 保存/重排不取锁。
                # 显式 BEGIN IMMEDIATE 取得写意向锁（pysqlite 的 SELECT 不开启
                # 物理事务，裸读不固定快照）：REST 写入会阻塞到本事务提交
                # （busy_timeout 30s），保证校准读与排序写入原子。锁内快照之后
                # 若有并发提交，以最新已提交状态复核——序号分布不一致说明卷
                # 结构已变，拒绝执行让调用方重试；仅正文等元数据变化则刷新
                # 快照，保证修订记录不含过期正文（回滚才不会覆盖并发保存）。
                await session.execute(text("BEGIN IMMEDIATE"))
                latest_rows = await chapter_repo.list_by_volume(session, volume_id)
                latest_by_id = {row.id: row for row in latest_rows}
                if set(latest_by_id) != set(before) or any(
                    latest_by_id[cid].order != image.order
                    for cid, image in before.items()
                ):
                    raise ToolExecutionError(
                        "卷内章节在重排期间被并发修改（章节集合或序号已变化），请重试"
                    )
                before = {
                    cid: replace(
                        image,
                        title=latest_by_id[cid].title,
                        content=latest_by_id[cid].content,
                        word_count=latest_by_id[cid].word_count,
                    )
                    for cid, image in before.items()
                }

                updated = await chapter_service.reorder_chapters(
                    session, volume_id, ordered_ids
                )
                # reorder 不改正文：以 before 快照为底，仅替换 order 字段，
                # 避免 metadata-only 对象访问 .content 触发同步 lazy load（MissingGreenlet）。
                after = {
                    chapter.id: replace(before[chapter.id], order=chapter.order)
                    for chapter in updated
                }
                affected = await record_chapter_diffs(
                    session,
                    revision_id=revision_id,
                    project_id=self.project_id,
                    before=before,
                    after=after,
                )
                for chapter_id in affected:
                    await record_agent_activity_for_change(
                        session,
                        revision_id=revision_id,
                        task_id=str(self._state.get("task_id") or ""),
                        agent_session_id=self.session_id,
                        before=before.get(chapter_id),
                        after=after.get(chapter_id),
                    )
                await refresh_project_stats(session, self.project_id)
                await session.commit()
                return json.dumps(
                    {
                        "success": True,
                        "metadata": {
                            "volume": volume_title,
                            "new_order": [
                                {
                                    "order": chapter.order,
                                    "title": chapter.title,
                                }
                                for chapter in sorted(
                                    updated, key=lambda chapter: chapter.order or 0
                                )
                            ],
                        },
                    },
                    ensure_ascii=False,
                )
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
