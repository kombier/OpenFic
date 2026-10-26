import json

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel, col

from app.agent_runtime.persistence.model import AgentAttachment, AgentRunMessage
from app.agent_runtime.persistence import repo as message_repo
from app.storage.models.chapter import Chapter
from app.storage.models.character import Character
from app.storage.models.note import Note, NoteCategory
from app.storage.models.project import Project
from app.storage.models.revision_chapter_snapshot import RevisionChapterSnapshot
from app.storage.models.task import Task
from app.storage.models.volume import Volume
from app.storage.models.world_info import WorldInfo
from app.storage.models.world_info_entry import WorldInfoEntry
from tests.model_registry import register_sqlmodel_models


@pytest_asyncio.fixture
async def revision_db(monkeypatch):
    register_sqlmodel_models()

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    async with factory() as session:
        session.add(Project(id="proj-1", title="测试项目"))
        session.add(
            Volume(
                id="vol-1",
                project_id="proj-1",
                title="第一卷",
                order=1,
                chapter_count=2,
            )
        )
        session.add(
            Chapter(
                id="chap-1",
                project_id="proj-1",
                volume_id="vol-1",
                title="第一章",
                content="旧内容一",
                word_count=4,
                order=1,
            )
        )
        session.add(
            Chapter(
                id="chap-2",
                project_id="proj-1",
                volume_id="vol-1",
                title="第二章",
                content="旧内容二",
                word_count=4,
                order=2,
            )
        )
        session.add(
            Task(
                id="task-1",
                project_id="proj-1",
                title="Agent Session",
                mode="agent",
                agent_session_id="sess-1",
            )
        )
        session.add(
            WorldInfo(
                id="world-1",
                project_id="proj-1",
                name="测试世界书",
            )
        )
        session.add(
            WorldInfoEntry(
                id="entry-1",
                world_info_id="world-1",
                uid=1,
                name="已有条目",
                order=1,
                content="原始设定",
                token_count=4,
                is_enabled=True,
            )
        )
        session.add(
            Character(
                id="char-1",
                project_id="proj-1",
                name="已有角色",
                description="原始角色描述",
                is_favorited=False,
            )
        )
        session.add(
            NoteCategory(
                id="cat-1",
                project_id="proj-1",
                title="设定",
            )
        )
        session.add(
            Note(
                id="note-1",
                project_id="proj-1",
                category_id="cat-1",
                title="已有笔记",
                content="原始内容",
            )
        )
        await session.commit()

    async def create_test_session():
        return factory()

    monkeypatch.setattr(
        "app.agent_runtime.tools.impls.chapter.write_chapter.create_session",
        create_test_session,
    )
    monkeypatch.setattr(
        "app.agent_runtime.tools.impls.chapter.delete_chapter.create_session",
        create_test_session,
    )
    monkeypatch.setattr(
        "app.agent_runtime.tools.impls.note.write_note.create_session",
        create_test_session,
    )
    monkeypatch.setattr(
        "app.agent_runtime.tools.impls.note.edit_note.create_session",
        create_test_session,
    )
    monkeypatch.setattr(
        "app.agent_runtime.tools.impls.note.delete_note.create_session",
        create_test_session,
    )
    monkeypatch.setattr(
        "app.agent_runtime.tools.impls.note.move_note.create_session",
        create_test_session,
    )
    monkeypatch.setattr(
        "app.agent_runtime.tools.impls.note.create_note_category.create_session",
        create_test_session,
    )
    monkeypatch.setattr(
        "app.agent_runtime.tools.impls.context.world_entry.create_session",
        create_test_session,
    )
    monkeypatch.setattr(
        "app.agent_runtime.tools.impls.context.character.create_session",
        create_test_session,
    )
    monkeypatch.setattr(
        "app.agent_runtime.tools.impls.chapter.reorder_chapters.create_session",
        create_test_session,
    )

    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_write_chapter_records_current_revision_and_structured_result(revision_db):
    from app.agent_runtime.revisions import begin_user_revision
    from app.agent_runtime.tools.impls.chapter.write_chapter import WriteChapterTool
    from app.storage.repos import commit_repo, revision_chapter_snapshot_repo

    async with revision_db() as session:
        user = await message_repo.insert_message(
            session,
            session_id="sess-1",
            task_id="task-1",
            project_id="proj-1",
            role="user",
            status="sent",
            content="插入一个章节",
        )
        revision = await begin_user_revision(
            session,
            project_id="proj-1",
            task_id="task-1",
            agent_session_id="sess-1",
            user_message_id=user.id,
            user_message_seq=user.seq,
            message="用户消息: 插入一个章节",
            pre_run_checkpoint_id="cp-before",
            graph_thread_id="sess-1",
        )
        await session.commit()

    tool = WriteChapterTool(
        _state={
            "session_id": "sess-1",
            "task_id": "task-1",
            "project_id": "proj-1",
            "current_revision_id": revision.id,
        }
    )
    result = await tool.ainvoke(
        {
            "volume_ref": {"type": "order", "value": 1},
            "title": "插入章",
            "content": "新内容",
            "chapter_ref": {"type": "order", "value": 2},
        }
    )

    payload = json.loads(result)
    chapter_diff = payload["metadata"]["chapter_diff"]
    assert payload["success"] is True
    assert payload["word_count"] == 3
    assert chapter_diff["chapter_title"] == "插入章"
    assert chapter_diff["order"] == 2

    async with revision_db() as session:
        chapters = await session.execute(
            Chapter.__table__.select().where(Chapter.project_id == "proj-1")
        )
        rows = chapters.mappings().all()
        inserted_row = next(row for row in rows if row["title"] == "插入章")
        commits = await commit_repo.list_by_revision(session, revision.id)
        snapshots = await revision_chapter_snapshot_repo.list_by_revision(
            session, revision.id
        )
    inserted_id = chapter_diff["chapter_id"]
    assert inserted_row["order"] == 2
    assert {(item.chapter_id, item.operation) for item in commits} == {
        (inserted_id, "create"),
        ("chap-2", "update"),
    }
    assert {(item.chapter_id, item.exists) for item in snapshots} == {
        (inserted_id, False),
        ("chap-2", True),
    }


@pytest.mark.asyncio
async def test_rollback_clears_prune_marks_created_by_rolled_back_revision(revision_db):
    from app.agent_runtime.revisions import begin_user_revision, rollback_revision_for_session

    async with revision_db() as session:
        await message_repo.insert_message(
            session,
            session_id="sess-1",
            task_id="task-1",
            project_id="proj-1",
            role="user",
            status="sent",
            content="初始请求",
        )
        await message_repo.insert_message(
            session,
            session_id="sess-1",
            task_id="task-1",
            project_id="proj-1",
            role="assistant",
            status="complete",
            content="调用工具",
            tool_calls=[{"id": "old-call", "name": "read_chapter", "args": {}}],
        )
        old_tool = await message_repo.insert_message(
            session,
            session_id="sess-1",
            task_id="task-1",
            project_id="proj-1",
            role="tool",
            status="complete",
            content="完整旧工具输出",
            tool_call_id="old-call",
            tool_name="read_chapter",
        )
        target_user = await message_repo.insert_message(
            session,
            session_id="sess-1",
            task_id="task-1",
            project_id="proj-1",
            role="user",
            status="sent",
            content="需要回滚的请求",
        )
        revision = await begin_user_revision(
            session,
            project_id="proj-1",
            task_id="task-1",
            agent_session_id="sess-1",
            user_message_id=target_user.id,
            user_message_seq=target_user.seq,
            message="用户消息: 需要回滚的请求",
            pre_run_checkpoint_id=None,
            graph_thread_id="sess-1",
        )
        await message_repo.mark_tool_messages_pruned(
            session,
            session_id="sess-1",
            tool_call_ids=[old_tool.tool_call_id or ""],
            revision_id=revision.id,
        )
        await session.commit()

        await rollback_revision_for_session(
            session,
            agent_session_id="sess-1",
            revision_id=revision.id,
        )
        await session.commit()

        restored_tool = await session.get(AgentRunMessage, old_tool.id)
        assert restored_tool is not None
        assert restored_tool.content == "完整旧工具输出"
        assert json.loads(restored_tool.message_metadata or "{}") == {}
        assert await session.get(AgentRunMessage, target_user.id) is None


@pytest.mark.asyncio
async def test_rollback_second_subagent_revision_restores_chapter_to_first_revision_content(
    revision_db,
    monkeypatch,
):
    from app.agent_runtime.revisions import begin_user_revision, rollback_revision_for_session
    from app.agent_runtime.tools.impls.chapter.edit_chapter import EditChapterTool
    from app.agent_runtime.tools.impls.chapter.write_chapter import WriteChapterTool
    from app.storage.repos import chapter_repo

    async def create_test_session():
        return revision_db()

    monkeypatch.setattr(
        "app.agent_runtime.tools.impls.chapter.edit_chapter.create_session",
        create_test_session,
    )

    async with revision_db() as session:
        first_user = await message_repo.insert_message(
            session,
            session_id="sess-1",
            task_id="task-1",
            project_id="proj-1",
            role="user",
            status="sent",
            content="委派 subagent 创建章节",
        )
        first_revision = await begin_user_revision(
            session,
            project_id="proj-1",
            task_id="task-1",
            agent_session_id="sess-1",
            user_message_id=first_user.id,
            user_message_seq=first_user.seq,
            message="用户消息: 委派 subagent 创建章节",
            pre_run_checkpoint_id="cp-before-first",
            graph_thread_id="sess-1",
        )
        await session.commit()

    write_tool = WriteChapterTool(
        _state={
            "session_id": "sess-1:child:writer",
            "task_id": "task-1",
            "project_id": "proj-1",
            "current_revision_id": first_revision.id,
        }
    )
    write_result = json.loads(
        await write_tool.ainvoke(
            {
                "volume_ref": {"type": "order", "value": 1},
                "title": "Subagent 章节",
                "content": "你好",
            }
        )
    )
    chapter_id = write_result["metadata"]["chapter_diff"]["chapter_id"]

    async with revision_db() as session:
        second_user = await message_repo.insert_message(
            session,
            session_id="sess-1",
            task_id="task-1",
            project_id="proj-1",
            role="user",
            status="sent",
            content="通知 subagent 修改章节",
        )
        second_revision = await begin_user_revision(
            session,
            project_id="proj-1",
            task_id="task-1",
            agent_session_id="sess-1",
            user_message_id=second_user.id,
            user_message_seq=second_user.seq,
            message="用户消息: 通知 subagent 修改章节",
            pre_run_checkpoint_id="cp-before-second",
            graph_thread_id="sess-1",
        )
        await session.commit()

    edit_tool = EditChapterTool(
        _state={
            "session_id": "sess-1:child:writer",
            "task_id": "task-1",
            "project_id": "proj-1",
            "current_revision_id": second_revision.id,
        }
    )
    edit_result = json.loads(
        await edit_tool.ainvoke(
            {
                "volume_ref": {"type": "order", "value": 1},
                "chapter_ref": {"type": "title", "value": "Subagent 章节"},
                "old_content": "你好",
                "new_content": "Hello",
            }
        )
    )
    assert edit_result.get("success") is True, edit_result

    async with revision_db() as session:
        edited = await chapter_repo.get_by_id(session, chapter_id)
        assert edited is not None
        assert edited.content == "Hello"
        await rollback_revision_for_session(
            session,
            agent_session_id="sess-1",
            revision_id=second_revision.id,
        )
        await session.commit()

    async with revision_db() as session:
        restored = await chapter_repo.get_by_id(session, chapter_id)

    assert restored is not None
    assert restored.content == "你好"


@pytest.mark.asyncio
async def test_rollback_revision_restores_chapters_and_messages(revision_db):
    from app.agent_runtime.revisions import (
        begin_user_revision,
        rollback_revision_for_session,
    )
    from app.agent_runtime.tools.impls.chapter.write_chapter import WriteChapterTool
    from app.storage.repos import revision_repo

    async with revision_db() as session:
        user = await message_repo.insert_message(
            session,
            session_id="sess-1",
            task_id="task-1",
            project_id="proj-1",
            role="user",
            status="sent",
            content="插入一个章节",
        )
        revision = await begin_user_revision(
            session,
            project_id="proj-1",
            task_id="task-1",
            agent_session_id="sess-1",
            user_message_id=user.id,
            user_message_seq=user.seq,
            message="用户消息: 插入一个章节",
            pre_run_checkpoint_id="cp-before",
            graph_thread_id="sess-1",
        )
        await session.commit()

    tool = WriteChapterTool(
        _state={
            "session_id": "sess-1",
            "task_id": "task-1",
            "project_id": "proj-1",
            "current_revision_id": revision.id,
        }
    )
    result = json.loads(
        await tool.ainvoke(
            {
                "volume_ref": {"type": "order", "value": 1},
                "title": "插入章",
                "content": "新内容",
                "chapter_ref": {"type": "order", "value": 2},
            }
        )
    )
    assert result["success"] is True
    inserted_id = result["metadata"]["chapter_diff"]["chapter_id"]

    async with revision_db() as session:
        await message_repo.insert_message(
            session,
            session_id="sess-1",
            task_id="task-1",
            project_id="proj-1",
            role="assistant",
            status="complete",
            content="已插入",
        )
        await session.commit()

    async with revision_db() as session:
        rollback_result = await rollback_revision_for_session(
            session,
            agent_session_id="sess-1",
            revision_id=revision.id,
        )
        await session.commit()

    assert rollback_result.restored_message_content == "插入一个章节"
    assert set(rollback_result.affected_chapters) == {inserted_id, "chap-2"}

    async with revision_db() as session:
        chapters = await session.execute(
            Chapter.__table__.select().where(Chapter.project_id == "proj-1")
        )
        rows = sorted(chapters.mappings().all(), key=lambda row: row["order"])
        messages = await message_repo.list_by_session(session, "sess-1")
        rolled_back = await revision_repo.get_by_id(session, revision.id)
        task = await session.get(Task, "task-1")

    assert [(row["id"], row["order"]) for row in rows] == [("chap-1", 1), ("chap-2", 2)]
    assert inserted_id not in {row["id"] for row in rows}
    assert messages == []
    assert rolled_back is not None
    assert rolled_back.status == "rolled_back"
    assert task is not None


@pytest.mark.asyncio
async def test_rollback_after_reorder_restores_swapped_orders(revision_db):
    """回归：重排交换序号后回滚不再触发 uq_chapters_volume_order 唯一约束冲突。

    旧实现逐章恢复 order：将 [A, B] 重排为 [B, A] 后回滚，恢复第一章的
    order 会先撞上另一章仍占用的序号，抛 UNIQUE constraint failed。
    """
    from app.agent_runtime.revisions import (
        begin_user_revision,
        rollback_revision_for_session,
    )
    from app.agent_runtime.tools.impls.chapter.reorder_chapters import (
        ReorderChaptersTool,
    )
    from app.storage.repos import commit_repo

    async with revision_db() as session:
        user = await message_repo.insert_message(
            session,
            session_id="sess-1",
            task_id="task-1",
            project_id="proj-1",
            role="user",
            status="sent",
            content="交换两章顺序",
        )
        revision = await begin_user_revision(
            session,
            project_id="proj-1",
            task_id="task-1",
            agent_session_id="sess-1",
            user_message_id=user.id,
            user_message_seq=user.seq,
            message="用户消息: 交换两章顺序",
            pre_run_checkpoint_id="cp-before",
            graph_thread_id="sess-1",
        )
        await session.commit()

    tool = ReorderChaptersTool(
        _state={
            "session_id": "sess-1",
            "task_id": "task-1",
            "project_id": "proj-1",
            "current_revision_id": revision.id,
        }
    )
    result = json.loads(
        await tool.ainvoke(
            {
                "volume_ref": {"type": "order", "value": 1},
                "ordered_chapter_refs": [
                    {"type": "title", "value": "第二章"},
                    {"type": "title", "value": "第一章"},
                ],
            }
        )
    )
    assert result["success"] is True

    async with revision_db() as session:
        rollback_result = await rollback_revision_for_session(
            session,
            agent_session_id="sess-1",
            revision_id=revision.id,
        )
        await session.commit()

    assert set(rollback_result.affected_chapters) == {"chap-1", "chap-2"}

    async with revision_db() as session:
        chapters = await session.execute(
            Chapter.__table__.select().where(Chapter.volume_id == "vol-1")
        )
        rows = sorted(chapters.mappings().all(), key=lambda row: row["order"])
        commits = await commit_repo.list_by_revision(
            session, rollback_result.rollback_revision.id
        )

    assert [(row["id"], row["order"]) for row in rows] == [("chap-1", 1), ("chap-2", 2)]
    # 回滚 Commit 的 before 快照必须记录腾位前的真实 order（重排后 chap-1=2、
    # chap-2=1），而非 Phase 1 腾位写入的负值。
    assert sorted(
        (commit.chapter_id, commit.snapshot_order, commit.new_order)
        for commit in commits
    ) == [("chap-1", 2, 1), ("chap-2", 1, 2)]


@pytest.mark.asyncio
async def test_rollback_after_content_edit_keeps_positive_orders(revision_db, monkeypatch):
    """回归：order 不变的正文回滚后，腾位负值不得残留。

    腾位 UPDATE 不回写 ORM 对象；若章节对象仍在 session 中存活，恢复时
    order 赋值与内存旧值净零相同，会被变更检测省略该列——腾位写入的负
    order 将残留入库。
    """
    from app.agent_runtime.revisions import (
        begin_user_revision,
        rollback_revision_for_session,
    )
    from app.agent_runtime.tools.impls.chapter.edit_chapter import EditChapterTool

    async def create_test_session():
        return revision_db()

    monkeypatch.setattr(
        "app.agent_runtime.tools.impls.chapter.edit_chapter.create_session",
        create_test_session,
    )

    async with revision_db() as session:
        user = await message_repo.insert_message(
            session,
            session_id="sess-1",
            task_id="task-1",
            project_id="proj-1",
            role="user",
            status="sent",
            content="改写第一章正文",
        )
        revision = await begin_user_revision(
            session,
            project_id="proj-1",
            task_id="task-1",
            agent_session_id="sess-1",
            user_message_id=user.id,
            user_message_seq=user.seq,
            message="用户消息: 改写第一章正文",
            pre_run_checkpoint_id="cp-before",
            graph_thread_id="sess-1",
        )
        await session.commit()

    tool = EditChapterTool(
        _state={
            "session_id": "sess-1",
            "task_id": "task-1",
            "project_id": "proj-1",
            "current_revision_id": revision.id,
        }
    )
    result = json.loads(
        await tool.ainvoke(
            {
                "volume_ref": {"type": "order", "value": 1},
                "chapter_ref": {"type": "title", "value": "第一章"},
                "old_content": "旧内容一",
                "new_content": "改写后的新内容",
            }
        )
    )
    assert result["success"] is True

    async with revision_db() as session:
        rollback_result = await rollback_revision_for_session(
            session,
            agent_session_id="sess-1",
            revision_id=revision.id,
        )
        await session.commit()

    assert set(rollback_result.affected_chapters) == {"chap-1"}

    async with revision_db() as session:
        chapters = await session.execute(
            Chapter.__table__.select().where(Chapter.volume_id == "vol-1")
        )
        rows = sorted(chapters.mappings().all(), key=lambda row: row["order"])

    assert [(row["id"], row["order"]) for row in rows] == [("chap-1", 1), ("chap-2", 2)]
    assert next(row for row in rows if row["id"] == "chap-1")["content"] == "旧内容一"


@pytest.mark.asyncio
async def test_reorder_snapshot_refreshes_rest_content_saved_during_lock(
    revision_db, monkeypatch
):
    """回归：REST 在锁内快照之后并发保存正文，修订快照必须取最新值。

    章节锁只约束 Agent 工具，REST 保存不取锁。锁内读取旧正文后、写前
    校准读到 REST 已提交的新正文——快照应刷新为新值，否则之后回滚会把
    并发保存的正文覆盖回旧文。
    """
    from sqlalchemy import update as sa_update

    from app.agent_runtime.revisions import begin_user_revision
    from app.agent_runtime.tools.impls.chapter import reorder_chapters as rc

    async with revision_db() as session:
        user = await message_repo.insert_message(
            session,
            session_id="sess-1",
            task_id="task-1",
            project_id="proj-1",
            role="user",
            status="sent",
            content="交换两章顺序",
        )
        revision = await begin_user_revision(
            session,
            project_id="proj-1",
            task_id="task-1",
            agent_session_id="sess-1",
            user_message_id=user.id,
            user_message_seq=user.seq,
            message="用户消息: 交换两章顺序",
            pre_run_checkpoint_id="cp-before",
            graph_thread_id="sess-1",
        )
        await session.commit()

    real_list_by_volume = rc.chapter_repo.list_by_volume
    call_state = {"count": 0}

    async def spy_list_by_volume(session, volume_id, **kwargs):
        call_state["count"] += 1
        rows = await real_list_by_volume(session, volume_id, **kwargs)
        if call_state["count"] == 1:
            # 第一次读取（锁内快照）完成后，模拟 REST 端并发保存正文。
            async with revision_db() as rest_session:
                await rest_session.execute(
                    sa_update(Chapter)
                    .where(col(Chapter.id) == "chap-1")
                    .values(content="REST 并发保存的新正文")
                )
                await rest_session.commit()
        return rows

    monkeypatch.setattr(
        rc.chapter_repo, "list_by_volume", spy_list_by_volume
    )

    tool = rc.ReorderChaptersTool(
        _state={
            "session_id": "sess-1",
            "task_id": "task-1",
            "project_id": "proj-1",
            "current_revision_id": revision.id,
        }
    )
    result = json.loads(
        await tool.ainvoke(
            {
                "volume_ref": {"type": "order", "value": 1},
                "ordered_chapter_refs": [
                    {"type": "title", "value": "第二章"},
                    {"type": "title", "value": "第一章"},
                ],
            }
        )
    )
    assert result["success"] is True

    async with revision_db() as session:
        snapshots = await session.execute(
            RevisionChapterSnapshot.__table__.select().where(
                RevisionChapterSnapshot.revision_id == revision.id
            )
        )
        contents = {
            row["chapter_id"]: row["content"] for row in snapshots.mappings().all()
        }

    assert contents["chap-1"] == "REST 并发保存的新正文"


@pytest.mark.asyncio
async def test_reorder_rejects_structural_change_during_lock(revision_db, monkeypatch):
    """回归：锁内快照后 REST 并发改变卷结构（序号变化），工具应拒绝执行。"""
    from sqlalchemy import update as sa_update

    from app.agent_runtime.revisions import begin_user_revision
    from app.agent_runtime.tools.impls.chapter import reorder_chapters as rc

    async with revision_db() as session:
        user = await message_repo.insert_message(
            session,
            session_id="sess-1",
            task_id="task-1",
            project_id="proj-1",
            role="user",
            status="sent",
            content="交换两章顺序",
        )
        revision = await begin_user_revision(
            session,
            project_id="proj-1",
            task_id="task-1",
            agent_session_id="sess-1",
            user_message_id=user.id,
            user_message_seq=user.seq,
            message="用户消息: 交换两章顺序",
            pre_run_checkpoint_id="cp-before",
            graph_thread_id="sess-1",
        )
        await session.commit()

    real_list_by_volume = rc.chapter_repo.list_by_volume
    call_state = {"count": 0}

    async def spy_list_by_volume(session, volume_id, **kwargs):
        call_state["count"] += 1
        rows = await real_list_by_volume(session, volume_id, **kwargs)
        if call_state["count"] == 1:
            # 模拟 REST 端并发重排：交换两章序号。
            async with revision_db() as rest_session:
                await rest_session.execute(
                    sa_update(Chapter)
                    .where(col(Chapter.id) == "chap-1")
                    .values(order=-1)
                )
                await rest_session.execute(
                    sa_update(Chapter)
                    .where(col(Chapter.id) == "chap-2")
                    .values(order=1)
                )
                await rest_session.execute(
                    sa_update(Chapter)
                    .where(col(Chapter.id) == "chap-1")
                    .values(order=2)
                )
                await rest_session.commit()
        return rows

    monkeypatch.setattr(rc.chapter_repo, "list_by_volume", spy_list_by_volume)

    tool = rc.ReorderChaptersTool(
        _state={
            "session_id": "sess-1",
            "task_id": "task-1",
            "project_id": "proj-1",
            "current_revision_id": revision.id,
        }
    )
    result = json.loads(
        await tool.ainvoke(
            {
                "volume_ref": {"type": "order", "value": 1},
                "ordered_chapter_refs": [
                    {"type": "title", "value": "第二章"},
                    {"type": "title", "value": "第一章"},
                ],
            }
        )
    )
    assert result["success"] is False
    assert "并发修改" in result.get("error", result.get("message", ""))


@pytest.mark.asyncio
async def test_rollback_after_multi_volume_rotation(revision_db):
    """回归：多卷各自重排 + 三章循环移位后回滚，全部恢复原序。

    覆盖腾位逻辑在跨卷 order 重复（两卷各有 order 1/2）下的正确性。
    """
    from app.agent_runtime.revisions import (
        begin_user_revision,
        rollback_revision_for_session,
    )
    from app.agent_runtime.tools.impls.chapter.reorder_chapters import (
        ReorderChaptersTool,
    )

    async with revision_db() as session:
        session.add(
            Volume(
                id="vol-2",
                project_id="proj-1",
                title="第二卷",
                order=2,
                chapter_count=3,
            )
        )
        session.add(
            Chapter(
                id="chap-3",
                project_id="proj-1",
                volume_id="vol-2",
                title="第三章",
                content="旧内容三",
                word_count=4,
                order=1,
            )
        )
        session.add(
            Chapter(
                id="chap-4",
                project_id="proj-1",
                volume_id="vol-2",
                title="第四章",
                content="旧内容四",
                word_count=4,
                order=2,
            )
        )
        session.add(
            Chapter(
                id="chap-5",
                project_id="proj-1",
                volume_id="vol-2",
                title="第五章",
                content="旧内容五",
                word_count=4,
                order=3,
            )
        )
        await session.commit()

    async with revision_db() as session:
        user = await message_repo.insert_message(
            session,
            session_id="sess-1",
            task_id="task-1",
            project_id="proj-1",
            role="user",
            status="sent",
            content="两卷各自循环移位",
        )
        revision = await begin_user_revision(
            session,
            project_id="proj-1",
            task_id="task-1",
            agent_session_id="sess-1",
            user_message_id=user.id,
            user_message_seq=user.seq,
            message="用户消息: 两卷各自循环移位",
            pre_run_checkpoint_id="cp-before",
            graph_thread_id="sess-1",
        )
        await session.commit()

    tool = ReorderChaptersTool(
        _state={
            "session_id": "sess-1",
            "task_id": "task-1",
            "project_id": "proj-1",
            "current_revision_id": revision.id,
        }
    )
    # 卷一交换 [chap-2, chap-1]；卷二三章循环移位 [chap-4, chap-5, chap-3]。
    for volume_order, titles in (
        (1, ["第二章", "第一章"]),
        (2, ["第四章", "第五章", "第三章"]),
    ):
        result = json.loads(
            await tool.ainvoke(
                {
                    "volume_ref": {"type": "order", "value": volume_order},
                    "ordered_chapter_refs": [
                        {"type": "title", "value": title} for title in titles
                    ],
                }
            )
        )
        assert result["success"] is True

    async with revision_db() as session:
        rollback_result = await rollback_revision_for_session(
            session,
            agent_session_id="sess-1",
            revision_id=revision.id,
        )
        await session.commit()

    assert set(rollback_result.affected_chapters) == {
        "chap-1",
        "chap-2",
        "chap-3",
        "chap-4",
        "chap-5",
    }

    async with revision_db() as session:
        chapters = await session.execute(
            Chapter.__table__.select().where(Chapter.project_id == "proj-1")
        )
        rows = sorted(
            chapters.mappings().all(), key=lambda row: (row["volume_id"], row["order"])
        )

    assert [(row["id"], row["volume_id"], row["order"]) for row in rows] == [
        ("chap-1", "vol-1", 1),
        ("chap-2", "vol-1", 2),
        ("chap-3", "vol-2", 1),
        ("chap-4", "vol-2", 2),
        ("chap-5", "vol-2", 3),
    ]


@pytest.mark.asyncio
async def test_reorder_fails_safe_when_rest_commits_after_calibration_read(
    tmp_path, monkeypatch
):
    """WAL 写意向锁实证：REST 无法穿透校准读与排序写之间。

    生产库为 WAL 模式（storage/database.py 建连 PRAGMA），pysqlite 的
    SELECT 不开启物理事务、裸读不固定快照，故工具在写前校准读之前显式
    BEGIN IMMEDIATE 取得写意向锁：REST 连接的写入被阻塞到工具提交
    （busy_timeout 30s）。本测试用两个独立物理连接的文件库复现生产隔离
    语义：REST 与工具并发提交时，工具完整原子地完成重排，REST 在工具提
    交后落库——修订快照不会被并发写穿透污染。
    """
    import asyncio
    import os
    import uuid

    from sqlalchemy import text as sa_text
    from sqlalchemy import update as sa_update
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlmodel import SQLModel as _SQLModel

    from tests.model_registry import register_sqlmodel_models

    register_sqlmodel_models()

    db_file = str(tmp_path / f"reorder-wal-{uuid.uuid4().hex}.db")
    tool_engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}", future=True)
    rest_engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}", future=True)
    # 两个引擎各开 WAL：journal_mode 是库属性，任一连接设置后持久化。
    for engine in (tool_engine, rest_engine):
        async with engine.connect() as conn:
            await conn.execute(sa_text("PRAGMA journal_mode=WAL"))
    async with tool_engine.begin() as conn:
        await conn.run_sync(_SQLModel.metadata.create_all)

    tool_factory = async_sessionmaker(tool_engine, expire_on_commit=False)

    async def seed():
        async with tool_factory() as session:
            session.add(Project(id="proj-1", title="测试项目"))
            session.add(
                Volume(
                    id="vol-1", project_id="proj-1", title="第一卷", order=1, chapter_count=2
                )
            )
            session.add(
                Chapter(
                    id="chap-1",
                    project_id="proj-1",
                    volume_id="vol-1",
                    title="第一章",
                    content="旧内容一",
                    word_count=4,
                    order=1,
                )
            )
            session.add(
                Chapter(
                    id="chap-2",
                    project_id="proj-1",
                    volume_id="vol-1",
                    title="第二章",
                    content="旧内容二",
                    word_count=4,
                    order=2,
                )
            )
            session.add(
                Task(
                    id="task-1",
                    project_id="proj-1",
                    title="Agent Session",
                    mode="agent",
                    agent_session_id="sess-1",
                )
            )
            await session.commit()

    await seed()

    from app.agent_runtime.revisions import begin_user_revision
    from app.agent_runtime.tools.impls.chapter import reorder_chapters as rc

    async with tool_factory() as session:
        user = await message_repo.insert_message(
            session,
            session_id="sess-1",
            task_id="task-1",
            project_id="proj-1",
            role="user",
            status="sent",
            content="交换两章顺序",
        )
        revision = await begin_user_revision(
            session,
            project_id="proj-1",
            task_id="task-1",
            agent_session_id="sess-1",
            user_message_id=user.id,
            user_message_seq=user.seq,
            message="用户消息: 交换两章顺序",
            pre_run_checkpoint_id="cp-before",
            graph_thread_id="sess-1",
        )
        await session.commit()

    real_list_by_volume = rc.chapter_repo.list_by_volume
    real_service_reorder = rc.chapter_service.reorder_chapters
    call_state = {"count": 0}
    rest_task_holder = {}
    probe_state = {"rest_finished_before_write": None}

    async def _rest_save_content():
        rest_factory = async_sessionmaker(rest_engine, expire_on_commit=False)
        async with rest_factory() as rest_session:
            await rest_session.execute(
                sa_update(Chapter)
                .where(col(Chapter.id) == "chap-1")
                .values(content="REST 与工具并发提交的新正文")
            )
            await rest_session.commit()

    async def spy_list_by_volume(session, volume_id, **kwargs):
        call_state["count"] += 1
        rows = await real_list_by_volume(session, volume_id, **kwargs)
        if call_state["count"] == 2:
            # 校准读进行中：工具此时应已持有 BEGIN IMMEDIATE 的写意向锁。
            # REST 写从独立物理连接发出——若锁在位，它会被阻塞到工具提交。
            rest_task_holder["task"] = asyncio.create_task(_rest_save_content())
            await asyncio.sleep(0.05)
        return rows

    async def spy_service_reorder(session, volume_id, ordered_ids):
        # 排序写入前探询：REST 若能在校准读与写入之间完成提交，说明写意向
        # 锁缺位（穿透）；被锁挡住则探询超时——这正是原子性的验证对象。
        try:
            await asyncio.wait_for(
                asyncio.shield(rest_task_holder["task"]), timeout=0.5
            )
            probe_state["rest_finished_before_write"] = True
        except asyncio.TimeoutError:
            probe_state["rest_finished_before_write"] = False
        return await real_service_reorder(session, volume_id, ordered_ids)

    monkeypatch.setattr(rc.chapter_repo, "list_by_volume", spy_list_by_volume)
    monkeypatch.setattr(rc.chapter_service, "reorder_chapters", spy_service_reorder)

    async def create_tool_session():
        return tool_factory()

    monkeypatch.setattr(
        rc, "create_session", create_tool_session
    )

    tool = rc.ReorderChaptersTool(
        _state={
            "session_id": "sess-1",
            "task_id": "task-1",
            "project_id": "proj-1",
            "current_revision_id": revision.id,
        }
    )
    result = json.loads(
        await tool.ainvoke(
            {
                "volume_ref": {"type": "order", "value": 1},
                "ordered_chapter_refs": [
                    {"type": "title", "value": "第二章"},
                    {"type": "title", "value": "第一章"},
                ],
            }
        )
    )
    # 工具持有写意向锁完成重排；REST 写等待锁释放后提交，两者都成功。
    # 断言与收尾整体保护：失败也回收后台 task 并释放两个引擎。
    try:
        assert result["success"] is True
        # REST 在工具提交前被锁挡住（穿透=失败）。
        assert probe_state["rest_finished_before_write"] is False
        await rest_task_holder["task"]
    except AssertionError:
        rest_task = rest_task_holder.get("task")
        if rest_task is not None and not rest_task.done():
            rest_task.cancel()
        raise

    async with tool_factory() as session:
        chapters = await session.execute(
            Chapter.__table__.select().where(Chapter.volume_id == "vol-1")
        )
        rows = sorted(chapters.mappings().all(), key=lambda row: row["order"])
        snapshots = await session.execute(
            RevisionChapterSnapshot.__table__.select().where(
                RevisionChapterSnapshot.revision_id == revision.id
            )
        )
        snapshot_contents = {
            row["chapter_id"]: row["content"]
            for row in snapshots.mappings().all()
        }

    # 重排生效、REST 正文最终落库、修订快照记录 BEGIN 时点的已提交正文
    # （REST 在工具提交后才落库；重排操作本身不会覆盖等待后提交的 REST
    # 保存，快照如实反映工具执行时点之前的状态）。
    try:
        assert [(row["id"], row["order"]) for row in rows] == [
            ("chap-2", 1),
            ("chap-1", 2),
        ]
        assert next(row for row in rows if row["id"] == "chap-1")["content"] == (
            "REST 与工具并发提交的新正文"
        )
        assert snapshot_contents["chap-1"] == "旧内容一"
    finally:
        await tool_engine.dispose()
        await rest_engine.dispose()
        if os.path.exists(db_file):
            os.remove(db_file)


@pytest.mark.asyncio
async def test_rollback_after_mixed_reorder_create_delete(revision_db):
    """回归：同 revision 内重排+建章+删章混合操作，一次回滚全部恢复。

    覆盖腾位与删除/重建路径交错：删除章（快照 exists=False）先处理释放
    序号，重建/恢复章与交换章的序号恢复互不冲突。
    """
    from app.agent_runtime.revisions import (
        begin_user_revision,
        rollback_revision_for_session,
    )
    from app.agent_runtime.tools.impls.chapter.delete_chapter import DeleteChapterTool
    from app.agent_runtime.tools.impls.chapter.reorder_chapters import (
        ReorderChaptersTool,
    )
    from app.agent_runtime.tools.impls.chapter.write_chapter import WriteChapterTool

    async with revision_db() as session:
        user = await message_repo.insert_message(
            session,
            session_id="sess-1",
            task_id="task-1",
            project_id="proj-1",
            role="user",
            status="sent",
            content="重排加建删混合",
        )
        revision = await begin_user_revision(
            session,
            project_id="proj-1",
            task_id="task-1",
            agent_session_id="sess-1",
            user_message_id=user.id,
            user_message_seq=user.seq,
            message="用户消息: 重排加建删混合",
            pre_run_checkpoint_id="cp-before",
            graph_thread_id="sess-1",
        )
        await session.commit()

    state = {
        "session_id": "sess-1",
        "task_id": "task-1",
        "project_id": "proj-1",
        "current_revision_id": revision.id,
    }
    # ① 在第一章前插入新章（插入后：插入章=1、第一章=2、原第二章=3）
    # → ② 删除原第二章 → ③ 反转顺序 [第一章, 插入章]
    write_result = json.loads(
        await WriteChapterTool(_state=state).ainvoke(
            {
                "volume_ref": {"type": "order", "value": 1},
                "title": "插入章",
                "content": "新内容",
                "chapter_ref": {"type": "order", "value": 1},
            }
        )
    )
    assert write_result["success"] is True
    inserted_id = write_result["metadata"]["chapter_diff"]["chapter_id"]
    delete_result = json.loads(
        await DeleteChapterTool(_state=state).ainvoke(
            {
                "volume_ref": {"type": "order", "value": 1},
                "chapter_ref": {"type": "title", "value": "第二章"},
            }
        )
    )
    assert delete_result["success"] is True

    async with revision_db() as session:
        before_rows = await session.execute(
            Chapter.__table__.select().where(Chapter.volume_id == "vol-1")
        )
    before_orders = sorted(
        (row["order"], row["id"]) for row in before_rows.mappings().all()
    )
    assert before_orders == [(1, inserted_id), (2, "chap-1")]

    reorder_result = json.loads(
        await ReorderChaptersTool(_state=state).ainvoke(
            {
                "volume_ref": {"type": "order", "value": 1},
                "ordered_chapter_refs": [
                    {"type": "title", "value": "第一章"},
                    {"type": "title", "value": "插入章"},
                ],
            }
        )
    )
    assert reorder_result["success"] is True

    async with revision_db() as session:
        rollback_result = await rollback_revision_for_session(
            session,
            agent_session_id="sess-1",
            revision_id=revision.id,
        )
        await session.commit()

    assert set(rollback_result.affected_chapters) == {"chap-1", "chap-2", inserted_id}

    async with revision_db() as session:
        chapters = await session.execute(
            Chapter.__table__.select().where(Chapter.volume_id == "vol-1")
        )
        rows = sorted(chapters.mappings().all(), key=lambda row: row["order"])

    assert [(row["id"], row["order"]) for row in rows] == [("chap-1", 1), ("chap-2", 2)]
    assert next(row for row in rows if row["id"] == "chap-2")["content"] == "旧内容二"


@pytest.mark.asyncio
async def test_rollback_preserves_target_message_attachments_for_resending(revision_db):
    from app.agent_runtime.revisions import (
        begin_user_revision,
        rollback_revision_for_session,
    )

    attachment_metadata = {
        "id": "attachment-1",
        "storage_name": "sess-1/attachment-1.png",
        "file_name": "reference.png",
        "mime_type": "image/png",
        "size_bytes": 12,
        "width": 2,
        "height": 3,
        "url": "/agent-attachments/sess-1/attachment-1.png",
    }
    async with revision_db() as session:
        session.add(
            AgentAttachment(
                id="attachment-1",
                session_id="sess-1",
                task_id="task-1",
                project_id="proj-1",
                storage_name="sess-1/attachment-1.png",
                file_name="reference.png",
                mime_type="image/png",
                size_bytes=12,
                width=2,
                height=3,
            )
        )
        user_message = await message_repo.insert_message(
            session,
            session_id="sess-1",
            task_id="task-1",
            project_id="proj-1",
            role="user",
            status="sent",
            content="请参考图片",
            metadata={"attachments": [attachment_metadata]},
        )
        revision = await begin_user_revision(
            session,
            project_id="proj-1",
            task_id="task-1",
            agent_session_id="sess-1",
            user_message_id=user_message.id,
            user_message_seq=user_message.seq,
            message="用户消息: 请参考图片",
            pre_run_checkpoint_id="cp-before",
            graph_thread_id="sess-1",
        )
        await session.commit()

    async with revision_db() as session:
        result = await rollback_revision_for_session(
            session,
            agent_session_id="sess-1",
            revision_id=revision.id,
        )
        await session.commit()

    assert result.restored_message_content == "请参考图片"
    assert result.restored_attachments == [attachment_metadata]

    async with revision_db() as session:
        assert await session.get(AgentAttachment, "attachment-1") is not None


@pytest.mark.asyncio
async def test_rollback_defers_removed_message_attachment_cleanup_until_startup(
    revision_db,
    monkeypatch,
    tmp_path,
):
    from app.agent_runtime.attachments import cleanup_orphaned_agent_attachment_files
    from app.agent_runtime.revisions import begin_user_revision, rollback_revision_for_session
    from app.settings import settings

    monkeypatch.setattr(settings, "agent_attachments_dir", tmp_path)
    target_path = tmp_path / "sess-1" / "target.png"
    removed_path = tmp_path / "sess-1" / "removed.png"
    target_path.parent.mkdir()
    target_path.write_bytes(b"target")
    removed_path.write_bytes(b"removed")
    target_metadata = {
        "id": "target-attachment",
        "storage_name": "sess-1/target.png",
        "file_name": "target.png",
        "mime_type": "image/png",
        "size_bytes": 6,
        "width": 2,
        "height": 3,
        "url": "/agent-attachments/sess-1/target.png",
    }
    removed_metadata = {
        "id": "removed-attachment",
        "storage_name": "sess-1/removed.png",
        "file_name": "removed.png",
        "mime_type": "image/png",
        "size_bytes": 7,
        "width": 2,
        "height": 3,
        "url": "/agent-attachments/sess-1/removed.png",
    }

    async with revision_db() as session:
        session.add_all(
            [
                AgentAttachment(
                    id="target-attachment",
                    session_id="sess-1",
                    task_id="task-1",
                    project_id="proj-1",
                    storage_name=target_metadata["storage_name"],
                    file_name="target.png",
                    mime_type="image/png",
                    size_bytes=6,
                    width=2,
                    height=3,
                ),
                AgentAttachment(
                    id="removed-attachment",
                    session_id="sess-1",
                    task_id="task-1",
                    project_id="proj-1",
                    storage_name=removed_metadata["storage_name"],
                    file_name="removed.png",
                    mime_type="image/png",
                    size_bytes=7,
                    width=2,
                    height=3,
                ),
            ]
        )
        target_message = await message_repo.insert_message(
            session,
            session_id="sess-1",
            task_id="task-1",
            project_id="proj-1",
            role="user",
            status="sent",
            content="请参考目标图片",
            metadata={"attachments": [target_metadata]},
        )
        revision = await begin_user_revision(
            session,
            project_id="proj-1",
            task_id="task-1",
            agent_session_id="sess-1",
            user_message_id=target_message.id,
            user_message_seq=target_message.seq,
            message="用户消息: 请参考目标图片",
            pre_run_checkpoint_id="cp-before",
            graph_thread_id="sess-1",
        )
        await message_repo.insert_message(
            session,
            session_id="sess-1",
            task_id="task-1",
            project_id="proj-1",
            role="user",
            status="sent",
            content="后续消息",
            metadata={"attachments": [removed_metadata]},
        )
        await session.commit()

    async with revision_db() as session:
        result = await rollback_revision_for_session(
            session,
            agent_session_id="sess-1",
            revision_id=revision.id,
        )
        await session.commit()

    assert result.restored_attachments == [target_metadata]
    async with revision_db() as session:
        assert await session.get(AgentAttachment, "target-attachment") is not None
        assert await session.get(AgentAttachment, "removed-attachment") is not None
        await cleanup_orphaned_agent_attachment_files(session)
        await session.commit()
        assert await session.get(AgentAttachment, "target-attachment") is None
        assert await session.get(AgentAttachment, "removed-attachment") is None

    assert not target_path.exists()
    assert not removed_path.exists()


@pytest.mark.asyncio
async def test_rollback_rejects_oversized_chapter_snapshot_before_mutating(revision_db):
    from app.agent_runtime.revisions import (
        begin_user_revision,
        rollback_revision_for_session,
    )
    from app.storage.repos import chapter_repo, revision_chapter_snapshot_repo, revision_repo

    async with revision_db() as session:
        user = await message_repo.insert_message(
            session,
            session_id="sess-1",
            task_id="task-1",
            project_id="proj-1",
            role="user",
            status="sent",
            content="恢复超限章节",
        )
        target_revision = await begin_user_revision(
            session,
            project_id="proj-1",
            task_id="task-1",
            agent_session_id="sess-1",
            user_message_id=user.id,
            user_message_seq=user.seq,
            message="用户消息: 恢复超限章节",
            pre_run_checkpoint_id="cp-before",
            graph_thread_id="sess-1",
        )
        await revision_chapter_snapshot_repo.create(
            session,
            RevisionChapterSnapshot(
                revision_id=target_revision.id,
                chapter_id="chap-1",
                project_id="proj-1",
                exists=True,
                title="第一章",
                content="\n".join("超限内容" for _ in range(2001)),
                word_count=0,
                chapter_order=1,
            ),
        )
        chapter = await chapter_repo.get_by_id(session, "chap-1")
        assert chapter is not None
        chapter.content = "回滚前内容"
        await chapter_repo.update_chapter(session, chapter)
        await session.commit()

    async with revision_db() as session:
        with pytest.raises(ValueError, match="内容超出限制"):
            await rollback_revision_for_session(
                session,
                agent_session_id="sess-1",
                revision_id=target_revision.id,
            )
        await session.commit()

    async with revision_db() as session:
        chapter = await chapter_repo.get_by_id(session, "chap-1")
        target_after = await revision_repo.get_by_id(session, target_revision.id)
        revisions = await revision_repo.list_by_agent_session_from_seq(
            session,
            "sess-1",
            target_revision.user_message_seq,
        )

    assert chapter is not None
    assert chapter.content == "回滚前内容"
    assert target_after is not None
    assert target_after.status != "rolled_back"
    assert all(revision.revision_type != "rollback" for revision in revisions)


@pytest.mark.asyncio
async def test_rollback_revision_deletes_compactions_intersecting_deleted_messages(
    revision_db,
):
    from app.agent_runtime.persistence import compaction_repo
    from app.agent_runtime.revisions import (
        begin_user_revision,
        rollback_revision_for_session,
    )

    async with revision_db() as session:
        await message_repo.insert_message(
            session,
            session_id="sess-1",
            task_id="task-1",
            project_id="proj-1",
            role="user",
            status="sent",
            content="第一轮",
        )
        await message_repo.insert_message(
            session,
            session_id="sess-1",
            task_id="task-1",
            project_id="proj-1",
            role="assistant",
            status="complete",
            content="第一轮回复",
        )
        await message_repo.insert_message(
            session,
            session_id="sess-1",
            task_id="task-1",
            project_id="proj-1",
            role="assistant",
            status="complete",
            content="第二轮前置回复",
        )
        target_user = await message_repo.insert_message(
            session,
            session_id="sess-1",
            task_id="task-1",
            project_id="proj-1",
            role="user",
            status="sent",
            content="回滚目标",
        )
        target_revision = await begin_user_revision(
            session,
            project_id="proj-1",
            task_id="task-1",
            agent_session_id="sess-1",
            user_message_id=target_user.id,
            user_message_seq=target_user.seq,
            message="用户消息: 回滚目标",
            pre_run_checkpoint_id="cp-before-target",
            graph_thread_id="sess-1",
        )
        await message_repo.insert_message(
            session,
            session_id="sess-1",
            task_id="task-1",
            project_id="proj-1",
            role="assistant",
            status="complete",
            content="将被删除的回复",
        )
        await compaction_repo.insert_compaction(
            session,
            session_id="sess-1",
            task_id="task-1",
            project_id="proj-1",
            start_seq=0,
            end_seq=1,
            summary="保留的压缩",
            trigger="manual",
        )
        await compaction_repo.insert_compaction(
            session,
            session_id="sess-1",
            task_id="task-1",
            project_id="proj-1",
            start_seq=2,
            end_seq=4,
            summary="与回滚删除范围相交的压缩",
            trigger="manual",
        )
        await session.commit()

    async with revision_db() as session:
        await rollback_revision_for_session(
            session,
            agent_session_id="sess-1",
            revision_id=target_revision.id,
        )
        await session.commit()

    async with revision_db() as session:
        rows = await compaction_repo.list_by_session(session, "sess-1")

    assert [(row.start_seq, row.end_seq, row.summary) for row in rows] == [
        (0, 1, "保留的压缩"),
    ]


@pytest.mark.asyncio
async def test_write_note_records_revision_snapshot(revision_db):
    from app.agent_runtime.revisions import begin_user_revision
    from app.agent_runtime.tools.impls.note.write_note import WriteNoteTool
    from app.storage.repos import revision_note_snapshot_repo

    async with revision_db() as session:
        user = await message_repo.insert_message(
            session,
            session_id="sess-1",
            task_id="task-1",
            project_id="proj-1",
            role="user",
            status="sent",
            content="创建一个笔记",
        )
        revision = await begin_user_revision(
            session,
            project_id="proj-1",
            task_id="task-1",
            agent_session_id="sess-1",
            user_message_id=user.id,
            user_message_seq=user.seq,
            message="用户消息: 创建一个笔记",
            pre_run_checkpoint_id="cp-before",
            graph_thread_id="sess-1",
        )
        await session.commit()

    tool = WriteNoteTool(
        _state={
            "session_id": "sess-1",
            "task_id": "task-1",
            "project_id": "proj-1",
            "current_revision_id": revision.id,
        }
    )
    result = json.loads(
        await tool.ainvoke(
            {"title": "新笔记", "content": "新内容", "category_ref": {"id": "cat-1"}}
        )
    )
    assert result["success"] is True
    created_id = result["metadata"]["note_diff"]["note_id"]

    async with revision_db() as session:
        snapshots = await revision_note_snapshot_repo.list_by_revision(
            session, revision.id
        )

    assert {(item.note_id, item.exists) for item in snapshots} == {
        (created_id, False),
    }


@pytest.mark.asyncio
async def test_rollback_revision_restores_notes(revision_db):
    from app.agent_runtime.revisions import (
        begin_user_revision,
        rollback_revision_for_session,
    )
    from app.agent_runtime.tools.impls.note.delete_note import DeleteNoteTool
    from app.agent_runtime.tools.impls.note.edit_note import EditNoteTool
    from app.agent_runtime.tools.impls.note.write_note import WriteNoteTool
    from app.storage.repos import note_repo

    async with revision_db() as session:
        user = await message_repo.insert_message(
            session,
            session_id="sess-1",
            task_id="task-1",
            project_id="proj-1",
            role="user",
            status="sent",
            content="操作笔记",
        )
        revision = await begin_user_revision(
            session,
            project_id="proj-1",
            task_id="task-1",
            agent_session_id="sess-1",
            user_message_id=user.id,
            user_message_seq=user.seq,
            message="用户消息: 操作笔记",
            pre_run_checkpoint_id="cp-before",
            graph_thread_id="sess-1",
        )
        await session.commit()

    state = {
        "session_id": "sess-1",
        "task_id": "task-1",
        "project_id": "proj-1",
        "current_revision_id": revision.id,
    }

    write_tool = WriteNoteTool(_state=state)
    write_result = json.loads(
        await write_tool.ainvoke(
            {"title": "临时笔记", "content": "将被回滚删除", "category_ref": {"id": "cat-1"}}
        )
    )
    assert write_result["success"] is True
    created_id = write_result["metadata"]["note_diff"]["note_id"]

    edit_tool = EditNoteTool(_state=state)
    edit_result = json.loads(
        await edit_tool.ainvoke(
            {
                "note_ref": {"id": "note-1"},
                "old_content": "原始内容",
                "new_content": "被修改的内容",
            }
        )
    )
    assert edit_result["success"] is True

    delete_tool = DeleteNoteTool(_state=state)
    delete_result = json.loads(
        await delete_tool.ainvoke({"note_ref": {"id": "note-1"}})
    )
    assert delete_result["success"] is True

    async with revision_db() as session:
        rollback_result = await rollback_revision_for_session(
            session,
            agent_session_id="sess-1",
            revision_id=revision.id,
        )
        await session.commit()

    assert set(rollback_result.affected_notes) == {created_id, "note-1"}

    async with revision_db() as session:
        created_after = await note_repo.get_by_id(session, created_id)
        original_after = await note_repo.get_by_id(session, "note-1")
        all_notes = await note_repo.list_by_project(session, "proj-1")

    assert created_after is None
    assert original_after is not None
    assert original_after.title == "已有笔记"
    assert original_after.content == "原始内容"
    assert {n.id for n in all_notes} == {"note-1"}


@pytest.mark.asyncio
async def test_rollback_revision_restores_moved_note(revision_db):
    from app.agent_runtime.revisions import (
        begin_user_revision,
        rollback_revision_for_session,
    )
    from app.agent_runtime.tools.impls.note.move_note import MoveNoteTool
    from app.storage.repos import note_repo

    async with revision_db() as session:
        session.add(
            NoteCategory(
                id="cat-2",
                project_id="proj-1",
                title="角色",
            )
        )
        await session.commit()

    async with revision_db() as session:
        user = await message_repo.insert_message(
            session,
            session_id="sess-1",
            task_id="task-1",
            project_id="proj-1",
            role="user",
            status="sent",
            content="移动笔记",
        )
        revision = await begin_user_revision(
            session,
            project_id="proj-1",
            task_id="task-1",
            agent_session_id="sess-1",
            user_message_id=user.id,
            user_message_seq=user.seq,
            message="用户消息: 移动笔记",
            pre_run_checkpoint_id="cp-before",
            graph_thread_id="sess-1",
        )
        await session.commit()

    move_tool = MoveNoteTool(
        _state={
            "session_id": "sess-1",
            "task_id": "task-1",
            "project_id": "proj-1",
            "current_revision_id": revision.id,
        }
    )
    move_result = json.loads(
        await move_tool.ainvoke(
            {"note_ref": {"id": "note-1"}, "target_category_ref": {"id": "cat-2"}}
        )
    )
    assert move_result["success"] is True

    async with revision_db() as session:
        await rollback_revision_for_session(
            session,
            agent_session_id="sess-1",
            revision_id=revision.id,
        )
        await session.commit()

    async with revision_db() as session:
        note = await note_repo.get_by_id(session, "note-1")

    assert note is not None
    assert note.category_id == "cat-1"


@pytest.mark.asyncio
async def test_create_note_category_records_revision_snapshot(revision_db):
    from app.agent_runtime.revisions import begin_user_revision
    from app.agent_runtime.tools.impls.note.create_note_category import (
        CreateNoteCategoryTool,
    )
    from app.storage.repos import revision_note_snapshot_repo

    async with revision_db() as session:
        user = await message_repo.insert_message(
            session,
            session_id="sess-1",
            task_id="task-1",
            project_id="proj-1",
            role="user",
            status="sent",
            content="创建一个分类",
        )
        revision = await begin_user_revision(
            session,
            project_id="proj-1",
            task_id="task-1",
            agent_session_id="sess-1",
            user_message_id=user.id,
            user_message_seq=user.seq,
            message="用户消息: 创建一个分类",
            pre_run_checkpoint_id="cp-before",
            graph_thread_id="sess-1",
        )
        await session.commit()

    tool = CreateNoteCategoryTool(
        _state={
            "session_id": "sess-1",
            "task_id": "task-1",
            "project_id": "proj-1",
            "current_revision_id": revision.id,
        }
    )
    result = json.loads(
        await tool.ainvoke({"title": "新分类", "parent_ref": {"id": "cat-1"}})
    )
    assert result["success"] is True
    created_id = result["metadata"]["category"]["id"]

    async with revision_db() as session:
        snapshots = (
            await revision_note_snapshot_repo.list_category_snapshots_by_revision(
                session, revision.id
            )
        )

    assert {(item.category_id, item.exists) for item in snapshots} == {
        (created_id, False),
    }


@pytest.mark.asyncio
async def test_rollback_revision_restores_note_categories(revision_db):
    from app.agent_runtime.revisions import (
        begin_user_revision,
        rollback_revision_for_session,
    )
    from app.agent_runtime.tools.impls.note.create_note_category import (
        CreateNoteCategoryTool,
    )
    from app.storage.repos import note_category_repo

    async with revision_db() as session:
        user = await message_repo.insert_message(
            session,
            session_id="sess-1",
            task_id="task-1",
            project_id="proj-1",
            role="user",
            status="sent",
            content="创建分类",
        )
        revision = await begin_user_revision(
            session,
            project_id="proj-1",
            task_id="task-1",
            agent_session_id="sess-1",
            user_message_id=user.id,
            user_message_seq=user.seq,
            message="用户消息: 创建分类",
            pre_run_checkpoint_id="cp-before",
            graph_thread_id="sess-1",
        )
        await session.commit()

    state = {
        "session_id": "sess-1",
        "task_id": "task-1",
        "project_id": "proj-1",
        "current_revision_id": revision.id,
    }

    create_tool = CreateNoteCategoryTool(_state=state)
    create_result = json.loads(
        await create_tool.ainvoke({"title": "临时分类", "parent_ref": {"id": "cat-1"}})
    )
    assert create_result["success"] is True
    created_id = create_result["metadata"]["category"]["id"]

    async with revision_db() as session:
        rollback_result = await rollback_revision_for_session(
            session,
            agent_session_id="sess-1",
            revision_id=revision.id,
        )
        await session.commit()

    assert set(rollback_result.affected_note_categories) == {created_id}

    async with revision_db() as session:
        created_after = await note_category_repo.get_by_id(session, created_id)
        all_cats = await note_category_repo.list_by_project(session, "proj-1")

    assert created_after is None
    assert {c.id for c in all_cats} == {"cat-1"}


@pytest.mark.asyncio
async def test_rollback_restores_nested_category_before_note(revision_db):
    from app.agent_runtime.revisions import (
        begin_user_revision,
        rollback_revision_for_session,
    )
    from app.agent_runtime.tools.impls.note.create_note_category import (
        CreateNoteCategoryTool,
    )
    from app.agent_runtime.tools.impls.note.write_note import WriteNoteTool
    from app.storage.repos import note_category_repo, note_repo

    async with revision_db() as session:
        user = await message_repo.insert_message(
            session,
            session_id="sess-1",
            task_id="task-1",
            project_id="proj-1",
            role="user",
            status="sent",
            content="创建嵌套分类和笔记",
        )
        revision = await begin_user_revision(
            session,
            project_id="proj-1",
            task_id="task-1",
            agent_session_id="sess-1",
            user_message_id=user.id,
            user_message_seq=user.seq,
            message="用户消息: 创建嵌套分类和笔记",
            pre_run_checkpoint_id="cp-before",
            graph_thread_id="sess-1",
        )
        await session.commit()

    state = {
        "session_id": "sess-1",
        "task_id": "task-1",
        "project_id": "proj-1",
        "current_revision_id": revision.id,
    }

    cat_tool = CreateNoteCategoryTool(_state=state)
    cat_result = json.loads(
        await cat_tool.ainvoke({"title": "新父分类"})
    )
    assert cat_result["success"] is True
    new_cat_id = cat_result["metadata"]["category"]["id"]

    sub_cat_tool = CreateNoteCategoryTool(_state=state)
    sub_cat_result = json.loads(
        await sub_cat_tool.ainvoke(
            {"title": "子分类", "parent_ref": {"id": new_cat_id}}
        )
    )
    assert sub_cat_result["success"] is True
    sub_cat_id = sub_cat_result["metadata"]["category"]["id"]

    write_tool = WriteNoteTool(_state=state)
    write_result = json.loads(
        await write_tool.ainvoke(
            {"title": "嵌套笔记", "content": "内容", "category_ref": {"id": sub_cat_id}}
        )
    )
    assert write_result["success"] is True
    note_id = write_result["metadata"]["note_diff"]["note_id"]

    async with revision_db() as session:
        rollback_result = await rollback_revision_for_session(
            session,
            agent_session_id="sess-1",
            revision_id=revision.id,
        )
        await session.commit()

    assert set(rollback_result.affected_note_categories) == {
        new_cat_id,
        sub_cat_id,
    }
    assert set(rollback_result.affected_notes) == {note_id}

    async with revision_db() as session:
        new_cat_after = await note_category_repo.get_by_id(session, new_cat_id)
        sub_cat_after = await note_category_repo.get_by_id(session, sub_cat_id)
        note_after = await note_repo.get_by_id(session, note_id)
        all_cats = await note_category_repo.list_by_project(session, "proj-1")

    assert new_cat_after is None
    assert sub_cat_after is None
    assert note_after is None
    assert {c.id for c in all_cats} == {"cat-1"}


@pytest.mark.asyncio
async def test_rollback_revision_restores_created_world_entries(revision_db):
    from app.agent_runtime.revisions import (
        begin_user_revision,
        rollback_revision_for_session,
    )
    from app.agent_runtime.tools.impls.context.world_entry import CreateWorldEntryTool
    from app.storage.repos import world_info_entry_repo

    async with revision_db() as session:
        user = await message_repo.insert_message(
            session,
            session_id="sess-1",
            task_id="task-1",
            project_id="proj-1",
            role="user",
            status="sent",
            content="创建世界书条目",
        )
        revision = await begin_user_revision(
            session,
            project_id="proj-1",
            task_id="task-1",
            agent_session_id="sess-1",
            user_message_id=user.id,
            user_message_seq=user.seq,
            message="用户消息: 创建世界书条目",
            pre_run_checkpoint_id="cp-before",
            graph_thread_id="sess-1",
        )
        await session.commit()

    tool = CreateWorldEntryTool(
        _state={
            "session_id": "sess-1",
            "task_id": "task-1",
            "project_id": "proj-1",
            "current_revision_id": revision.id,
        }
    )
    result = json.loads(
        await tool.ainvoke({"title": "临时条目", "content": "临时设定"})
    )
    assert result["success"] is True
    entry_id = result["metadata"]["world_entry_diff"]["entry_id"]

    async with revision_db() as session:
        rollback_result = await rollback_revision_for_session(
            session,
            agent_session_id="sess-1",
            revision_id=revision.id,
        )
        await session.commit()

    assert set(rollback_result.affected_world_entries) == {entry_id}

    async with revision_db() as session:
        entry_after = await world_info_entry_repo.get_by_id(session, entry_id)

    assert entry_after is None


@pytest.mark.asyncio
async def test_rollback_revision_restores_edited_world_entries(revision_db):
    from app.agent_runtime.revisions import (
        begin_user_revision,
        rollback_revision_for_session,
    )
    from app.agent_runtime.tools.impls.context.world_entry import EditWorldEntryTool
    from app.storage.repos import world_info_entry_repo

    async with revision_db() as session:
        user = await message_repo.insert_message(
            session,
            session_id="sess-1",
            task_id="task-1",
            project_id="proj-1",
            role="user",
            status="sent",
            content="编辑世界书条目",
        )
        revision = await begin_user_revision(
            session,
            project_id="proj-1",
            task_id="task-1",
            agent_session_id="sess-1",
            user_message_id=user.id,
            user_message_seq=user.seq,
            message="用户消息: 编辑世界书条目",
            pre_run_checkpoint_id="cp-before",
            graph_thread_id="sess-1",
        )
        await session.commit()

    tool = EditWorldEntryTool(
        _state={
            "session_id": "sess-1",
            "task_id": "task-1",
            "project_id": "proj-1",
            "current_revision_id": revision.id,
        }
    )
    result = json.loads(
        await tool.ainvoke(
            {
                "title": "已有条目",
                "new_title": "改名条目",
                "old_content": "原始",
                "new_content": "更新",
            }
        )
    )
    assert result["success"] is True

    async with revision_db() as session:
        rollback_result = await rollback_revision_for_session(
            session,
            agent_session_id="sess-1",
            revision_id=revision.id,
        )
        await session.commit()

    assert set(rollback_result.affected_world_entries) == {"entry-1"}

    async with revision_db() as session:
        entry_after = await world_info_entry_repo.get_by_id(session, "entry-1")

    assert entry_after is not None
    assert entry_after.name == "已有条目"
    assert entry_after.content == "原始设定"


@pytest.mark.asyncio
async def test_rollback_revision_restores_deleted_world_entries(revision_db):
    from app.agent_runtime.revisions import (
        begin_user_revision,
        rollback_revision_for_session,
    )
    from app.agent_runtime.tools.impls.context.world_entry import DeleteWorldEntryTool
    from app.storage.repos import world_info_entry_repo

    async with revision_db() as session:
        user = await message_repo.insert_message(
            session,
            session_id="sess-1",
            task_id="task-1",
            project_id="proj-1",
            role="user",
            status="sent",
            content="删除世界书条目",
        )
        revision = await begin_user_revision(
            session,
            project_id="proj-1",
            task_id="task-1",
            agent_session_id="sess-1",
            user_message_id=user.id,
            user_message_seq=user.seq,
            message="用户消息: 删除世界书条目",
            pre_run_checkpoint_id="cp-before",
            graph_thread_id="sess-1",
        )
        await session.commit()

    tool = DeleteWorldEntryTool(
        _state={
            "session_id": "sess-1",
            "task_id": "task-1",
            "project_id": "proj-1",
            "current_revision_id": revision.id,
        }
    )
    result = json.loads(await tool.ainvoke({"title": "已有条目"}))
    assert result["success"] is True

    async with revision_db() as session:
        rollback_result = await rollback_revision_for_session(
            session,
            agent_session_id="sess-1",
            revision_id=revision.id,
        )
        await session.commit()

    assert set(rollback_result.affected_world_entries) == {"entry-1"}

    async with revision_db() as session:
        entry_after = await world_info_entry_repo.get_by_id(session, "entry-1")

    assert entry_after is not None
    assert entry_after.name == "已有条目"
    assert entry_after.content == "原始设定"


@pytest.mark.asyncio
async def test_rollback_revision_restores_created_characters(revision_db):
    from app.agent_runtime.revisions import (
        begin_user_revision,
        rollback_revision_for_session,
    )
    from app.agent_runtime.tools.impls.context.character import CreateCharacterTool
    from app.storage.repos import character_repo

    async with revision_db() as session:
        user = await message_repo.insert_message(
            session,
            session_id="sess-1",
            task_id="task-1",
            project_id="proj-1",
            role="user",
            status="sent",
            content="创建角色",
        )
        revision = await begin_user_revision(
            session,
            project_id="proj-1",
            task_id="task-1",
            agent_session_id="sess-1",
            user_message_id=user.id,
            user_message_seq=user.seq,
            message="用户消息: 创建角色",
            pre_run_checkpoint_id="cp-before",
            graph_thread_id="sess-1",
        )
        await session.commit()

    tool = CreateCharacterTool(
        _state={
            "session_id": "sess-1",
            "task_id": "task-1",
            "project_id": "proj-1",
            "current_revision_id": revision.id,
        }
    )
    result = json.loads(await tool.ainvoke({"name": "临时角色", "description": "临时描述"}))
    assert result["success"] is True
    character_id = result["metadata"]["character_diff"]["character_id"]

    async with revision_db() as session:
        rollback_result = await rollback_revision_for_session(
            session,
            agent_session_id="sess-1",
            revision_id=revision.id,
        )
        await session.commit()

    assert set(rollback_result.affected_characters) == {character_id}

    async with revision_db() as session:
        character_after = await character_repo.get_by_id(session, character_id)

    assert character_after is None


@pytest.mark.asyncio
async def test_rollback_revision_removes_created_character_relationship(revision_db):
    from app.agent_runtime.revisions import begin_user_revision, rollback_revision_for_session
    from app.agent_runtime.tools.impls.context.character import CreateCharacterRelationshipTool
    from app.storage.repos import character_relationship_repo

    async with revision_db() as session:
        session.add(Character(id="char-2", project_id="proj-1", name="第二角色"))
        user = await message_repo.insert_message(
            session, session_id="sess-1", task_id="task-1", project_id="proj-1",
            role="user", status="sent", content="建立关系",
        )
        revision = await begin_user_revision(
            session, project_id="proj-1", task_id="task-1", agent_session_id="sess-1",
            user_message_id=user.id, user_message_seq=user.seq, message="用户消息: 建立关系",
            pre_run_checkpoint_id="cp-before", graph_thread_id="sess-1",
        )
        await session.commit()

    tool = CreateCharacterRelationshipTool(_state={
        "session_id": "sess-1", "task_id": "task-1", "project_id": "proj-1",
        "current_revision_id": revision.id,
    })
    result = json.loads(await tool.ainvoke({
        "source_name": "已有角色", "target_name": "第二角色", "name": "朋友",
    }))
    assert result["success"] is True

    async with revision_db() as session:
        assert len(await character_relationship_repo.list_for_project(session, "proj-1")) == 1
        await rollback_revision_for_session(session, agent_session_id="sess-1", revision_id=revision.id)
        await session.commit()

    async with revision_db() as session:
        assert await character_relationship_repo.list_for_project(session, "proj-1") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["edit", "delete"])
async def test_rollback_revision_restores_existing_character_relationship(revision_db, operation):
    from app.agent_runtime.revisions import begin_user_revision, rollback_revision_for_session
    from app.agent_runtime.tools.impls.context.character import EditCharacterRelationshipTool, DeleteCharacterRelationshipTool
    from app.storage.repos import character_relationship_repo
    from app.storage.services import character_relationship_service

    async with revision_db() as session:
        session.add(Character(id="char-2", project_id="proj-1", name="第二角色"))
        await session.flush()
        relation = await character_relationship_service.create_relationship(session, "proj-1", "char-1", "char-2", "旧关系", "原说明")
        relation_id = relation.id
        user = await message_repo.insert_message(
            session, session_id="sess-1", task_id="task-1", project_id="proj-1",
            role="user", status="sent", content="修改关系",
        )
        revision = await begin_user_revision(
            session, project_id="proj-1", task_id="task-1", agent_session_id="sess-1",
            user_message_id=user.id, user_message_seq=user.seq, message="用户消息: 修改关系",
            pre_run_checkpoint_id="cp-before", graph_thread_id="sess-1",
        )
        await session.commit()

    tool_class = EditCharacterRelationshipTool if operation == "edit" else DeleteCharacterRelationshipTool
    tool = tool_class(_state={
        "session_id": "sess-1", "task_id": "task-1", "project_id": "proj-1", "current_revision_id": revision.id,
    })
    args = {"source_name": "已有角色", "target_name": "第二角色"}
    if operation == "edit":
        args.update(name="新关系", description="新说明")
    assert json.loads(await tool.ainvoke(args))["success"] is True

    async with revision_db() as session:
        await rollback_revision_for_session(session, agent_session_id="sess-1", revision_id=revision.id)
        await session.commit()

    async with revision_db() as session:
        restored = await character_relationship_repo.get(session, relation_id)
        assert restored is not None
        assert (restored.name, restored.description) == ("旧关系", "原说明")


@pytest.mark.asyncio
async def test_agent_queries_character_relationship_paths(revision_db):
    from app.agent_runtime.tools.impls.context.character import QueryCharacterRelationshipsTool, ReadCharacterTool
    from app.storage.services import character_relationship_service

    async with revision_db() as session:
        session.add_all([
            Character(id="char-2", project_id="proj-1", name="中转"),
            Character(id="char-3", project_id="proj-1", name="终点"),
        ])
        await session.flush()
        await character_relationship_service.create_relationship(session, "proj-1", "char-1", "char-2", "朋友")
        await character_relationship_service.create_relationship(session, "proj-1", "char-2", "char-3", "同盟")
        await session.commit()

    state = {"session_id": "sess-1", "project_id": "proj-1"}
    read = json.loads(await ReadCharacterTool(_state=state).ainvoke({"name": "已有角色"}))
    assert read["relationships"][0]["character_name"] == "中转"
    query = json.loads(await QueryCharacterRelationshipsTool(_state=state).ainvoke({
        "name": "已有角色", "target_name": "终点", "max_hops": 2,
    }))
    assert [step["name"] for step in query["paths"][0]] == ["朋友", "同盟"]


@pytest.mark.asyncio
async def test_rollback_revision_restores_edited_characters(revision_db):
    from app.agent_runtime.revisions import (
        begin_user_revision,
        rollback_revision_for_session,
    )
    from app.agent_runtime.tools.impls.context.character import EditCharacterTool
    from app.storage.repos import character_repo

    async with revision_db() as session:
        user = await message_repo.insert_message(
            session,
            session_id="sess-1",
            task_id="task-1",
            project_id="proj-1",
            role="user",
            status="sent",
            content="编辑角色",
        )
        revision = await begin_user_revision(
            session,
            project_id="proj-1",
            task_id="task-1",
            agent_session_id="sess-1",
            user_message_id=user.id,
            user_message_seq=user.seq,
            message="用户消息: 编辑角色",
            pre_run_checkpoint_id="cp-before",
            graph_thread_id="sess-1",
        )
        await session.commit()

    tool = EditCharacterTool(
        _state={
            "session_id": "sess-1",
            "task_id": "task-1",
            "project_id": "proj-1",
            "current_revision_id": revision.id,
        }
    )
    result = json.loads(
        await tool.ainvoke(
            {
                "name": "已有角色",
                "new_name": "改名角色",
                "old_description": "原始",
                "new_description": "更新",
            }
        )
    )
    assert result["success"] is True

    async with revision_db() as session:
        rollback_result = await rollback_revision_for_session(
            session,
            agent_session_id="sess-1",
            revision_id=revision.id,
        )
        await session.commit()

    assert set(rollback_result.affected_characters) == {"char-1"}

    async with revision_db() as session:
        character_after = await character_repo.get_by_id(session, "char-1")

    assert character_after is not None
    assert character_after.name == "已有角色"
    assert character_after.description == "原始角色描述"
    assert character_after.is_favorited is False


@pytest.mark.asyncio
async def test_rollback_revision_restores_deleted_characters(revision_db):
    from app.agent_runtime.revisions import (
        begin_user_revision,
        rollback_revision_for_session,
    )
    from app.agent_runtime.tools.impls.context.character import DeleteCharacterTool
    from app.storage.repos import character_repo

    async with revision_db() as session:
        user = await message_repo.insert_message(
            session,
            session_id="sess-1",
            task_id="task-1",
            project_id="proj-1",
            role="user",
            status="sent",
            content="删除角色",
        )
        revision = await begin_user_revision(
            session,
            project_id="proj-1",
            task_id="task-1",
            agent_session_id="sess-1",
            user_message_id=user.id,
            user_message_seq=user.seq,
            message="用户消息: 删除角色",
            pre_run_checkpoint_id="cp-before",
            graph_thread_id="sess-1",
        )
        await session.commit()

    tool = DeleteCharacterTool(
        _state={
            "session_id": "sess-1",
            "task_id": "task-1",
            "project_id": "proj-1",
            "current_revision_id": revision.id,
        }
    )
    result = json.loads(await tool.ainvoke({"name": "已有角色"}))
    assert result["success"] is True

    async with revision_db() as session:
        rollback_result = await rollback_revision_for_session(
            session,
            agent_session_id="sess-1",
            revision_id=revision.id,
        )
        await session.commit()

    assert set(rollback_result.affected_characters) == {"char-1"}

    async with revision_db() as session:
        character_after = await character_repo.get_by_id(session, "char-1")

    assert character_after is not None
    assert character_after.name == "已有角色"
    assert character_after.description == "原始角色描述"
