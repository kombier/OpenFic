# -*- coding: utf-8 -*-
"""
Setting Repository - 设置数据访问层。
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.storage.models.setting import Setting
from app.models.managed import read_catalog, require_manual_models, require_persistent_reference


async def get_all(session: AsyncSession) -> list[Setting]:
    """
    获取所有设置。

    Args:
        session: 数据库 session。

    Returns:
        设置列表。
    """
    result = await session.execute(select(Setting))
    rows = list(result.scalars().all())
    if catalog := read_catalog(session):
        rows = [s for s in rows if s.key != "default_model"]
        rows.append(Setting(key="default_model", value=await catalog.default_id(session)))
    return rows


async def get_by_key(session: AsyncSession, key: str) -> Setting | None:
    """
    根据键名获取设置。

    Args:
        session: 数据库 session。
        key: 设置键名。

    Returns:
        设置实例，如果不存在则返回 None。
    """
    if key == "default_model" and (catalog := read_catalog(session)):
        return Setting(key=key, value=await catalog.default_id(session))
    result = await session.execute(select(Setting).where(col(Setting.key) == key))
    return result.scalar_one_or_none()


async def upsert(session: AsyncSession, key: str, value: str) -> Setting:
    """
    创建或更新设置。

    Args:
        session: 数据库 session。
        key: 设置键名。
        value: 设置值。

    Returns:
        设置实例。
    """
    if key in {"light_model", "summary_model", "compaction_model"}:
        require_persistent_reference(value)
    if key == "default_model":
        require_manual_models(session)
    existing = await get_by_key(session, key)
    if existing:
        existing.value = value
        existing.updated_at = datetime.now(UTC)
        session.add(existing)
        await session.flush()
        await session.refresh(existing)
        return existing
    else:
        setting = Setting(key=key, value=value)
        session.add(setting)
        await session.flush()
        await session.refresh(setting)
        return setting


async def bulk_upsert(session: AsyncSession, settings: dict[str, str]) -> list[Setting]:
    """
    批量创建或更新设置。

    Args:
        session: 数据库 session。
        settings: 设置键值对字典。

    Returns:
        更新后的设置列表。
    """
    result = []
    for key, value in settings.items():
        setting = await upsert(session, key, value)
        result.append(setting)
    return result
