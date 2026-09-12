"""Audit log + key/value settings store (persisted in the DB)."""

from __future__ import annotations

from sqlalchemy import select

from . import security
from .db import sessions
from .models import AuditLog, Setting


async def audit(user: str, action: str) -> None:
    from .models import now_str

    async with sessions()() as db:
        db.add(AuditLog(id=security.new_id(), time=now_str(), user=user or "system", action=action))
        await db.commit()


DEFAULT_SETTINGS: dict[str, str] = {
    "telegram_bot_token": "",
    "telegram_admin_chat_id": "",
    "notify_expiry": "1",
    "notify_limit": "1",
    "notify_node_offline": "0",
    "node_poll_interval": "60",       # seconds between node status probes
    "traffic_poll_interval": "300",   # seconds between traffic collections
    "panel_url": "",                  # public URL used in Telegram messages
}


async def get_all_settings() -> dict[str, str]:
    result = dict(DEFAULT_SETTINGS)
    async with sessions()() as db:
        rows = await db.execute(select(Setting))
        for row in rows.scalars():
            result[row.key] = row.value or ""
    return result


async def get_setting(key: str) -> str:
    values = await get_all_settings()
    return values.get(key, "")


async def set_setting(key: str, value: str) -> None:
    async with sessions()() as db:
        existing = await db.get(Setting, key)
        if existing:
            existing.value = value
        else:
            db.add(Setting(key=key, value=value))
        await db.commit()


async def get_int_setting(key: str, default: int) -> int:
    try:
        return int(await get_setting(key) or default)
    except ValueError:
        return default
