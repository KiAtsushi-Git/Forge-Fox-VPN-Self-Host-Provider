"""Outgoing Telegram notifications to the admin chat (best-effort)."""

from __future__ import annotations

import logging

from . import audit as audit_mod

log = logging.getLogger("forgefox.telegram")


async def notify_admin(text: str) -> None:
    token = await audit_mod.get_setting("telegram_bot_token")
    chat_id = await audit_mod.get_setting("telegram_admin_chat_id")
    if not token or not chat_id:
        return
    try:
        from .telegram import call

        await call(token, "sendMessage", chat_id=int(chat_id), text=text)
    except Exception as e:
        log.warning("Telegram notification failed: %s", e)
