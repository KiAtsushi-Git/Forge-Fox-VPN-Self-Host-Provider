"""Telegram bot + notifications.

Long-polling bot (getUpdates) so no public webhook URL is needed — the panel
is often deployed on a bare IP. The bot starts automatically once a token is
saved in Settings and restarts itself when the token changes.

Commands:
  /start  — register this chat as the admin chat
  /help   — command list
  /stats  — dashboard counters
  /users  — list clients with status/traffic
  /add <username> <node name> — create a user on a node
  /del <username> — delete a user
  /sub <username> — send the subscription link
"""

from __future__ import annotations

import logging

import httpx
from sqlalchemy import select

from . import audit as audit_mod
from .db import sessions
from .models import Client, Node
from .provisioning import create_client
from .telegram_notify import notify_admin

log = logging.getLogger("forgefox.telegram")

API = "https://api.telegram.org/bot{token}/{method}"


async def call(token: str, method: str, **params) -> dict:
    async with httpx.AsyncClient(timeout=35) as client:
        resp = await client.post(API.format(token=token, method=method), json=params)
        return resp.json()


async def handle_command(token: str, chat_id: int, text: str) -> None:
    parts = text.split()
    cmd = parts[0].split("@")[0].lower()

    if cmd == "/start":
        await audit_mod.set_setting("telegram_admin_chat_id", str(chat_id))
        await call(token, "sendMessage", chat_id=chat_id, text=(
            "🦊 ForgeFox: этот чат привязан к панели.\n/help — список команд."
        ))
        return

    if cmd == "/help":
        await call(token, "sendMessage", chat_id=chat_id, text=(
            "🦊 ForgeFox Provider — команды:\n"
            "/stats — сводка панели\n"
            "/users — список пользователей\n"
            "/add <имя> <нода> — создать пользователя\n"
            "/del <имя> — удалить пользователя\n"
            "/sub <имя> — ссылка-подписка"
        ))
        return

    if cmd == "/stats":
        async with sessions()() as db:
            nodes = list((await db.execute(select(Node))).scalars())
            clients = list((await db.execute(select(Client))).scalars())
        online = sum(1 for n in nodes if n.status == "online")
        active = sum(1 for c in clients if (c.status or "active") == "active")
        blocked = sum(1 for c in clients if c.status in ("blocked", "expired", "limited"))
        await call(token, "sendMessage", chat_id=chat_id, text=(
            f"🦊 Ноды: {len(nodes)} (онлайн {online})\n"
            f"👥 Пользователи: {len(clients)} (активных {active}, заблокировано {blocked})"
        ))
        return

    if cmd == "/users":
        async with sessions()() as db:
            clients = list((await db.execute(select(Client))).scalars())
            nodes = {n.id: n for n in (await db.execute(select(Node))).scalars()}
        if not clients:
            await call(token, "sendMessage", chat_id=chat_id, text="Пользователей нет")
            return
        lines = []
        for c in clients[:60]:
            node_names = ", ".join(
                nodes[nid].name for nid in c.all_node_ids() if nid in nodes
            ) or "?"
            gb = (c.used_bytes or 0) / 1024**3
            limit = f"/{c.limit_gb}GB" if c.limit_gb else ""
            lines.append(f"• {c.username} [{c.status or 'active'}] {gb:.2f}{limit}GB — {node_names}")
        await call(token, "sendMessage", chat_id=chat_id, text="\n".join(lines))
        return

    if cmd == "/add":
        if len(parts) < 3:
            await call(token, "sendMessage", chat_id=chat_id,
                       text="Формат: /add <имя> <нода> (нода — часть названия)")
            return
        username, node_query = parts[1], " ".join(parts[2:]).lower()
        async with sessions()() as db:
            nodes = list((await db.execute(select(Node))).scalars())
        node = next((n for n in nodes if node_query in n.name.lower()), None)
        if node is None:
            await call(token, "sendMessage", chat_id=chat_id, text="Нода не найдена")
            return
        try:
            await create_client("telegram", username, [node.id])
            await call(token, "sendMessage", chat_id=chat_id,
                       text=f"✅ {username} создан на ноде {node.name} (/sub {username})")
        except ValueError as e:
            await call(token, "sendMessage", chat_id=chat_id, text=f"❌ {e}")
        return

    if cmd == "/del":
        if len(parts) < 2:
            return
        username = parts[1]
        async with sessions()() as db:
            rows = await db.execute(select(Client).where(Client.username == username))
            client = rows.scalars().first()
            if client is None:
                await call(token, "sendMessage", chat_id=chat_id, text="Не найден")
                return
            nodes = list((await db.execute(select(Node))).scalars())
            await db.delete(client)
            await db.commit()
        from . import ssh as ssh_mod

        for node in nodes:
            if node.id in client.all_node_ids():
                try:
                    await ssh_mod.delete_user(node, username)
                except ssh_mod.NodeError:
                    pass
        await call(token, "sendMessage", chat_id=chat_id, text=f"🗑 {username} удалён")
        return

    if cmd == "/sub":
        if len(parts) < 2:
            return
        username = parts[1]
        async with sessions()() as db:
            rows = await db.execute(select(Client).where(Client.username == username))
            client = rows.scalars().first()
        if client is None:
            await call(token, "sendMessage", chat_id=chat_id, text="Не найден")
            return
        panel_url = (await audit_mod.get_setting("panel_url")).rstrip("/")
        if panel_url:
            await call(token, "sendMessage", chat_id=chat_id,
                       text=f"🔗 {panel_url}/sub/{client.id}")
        else:
            await call(token, "sendMessage", chat_id=chat_id,
                       text="Не задан panel_url в настройках панели — ссылку возьмите в веб-интерфейсе.")
        return


async def bot_loop(stop_check) -> None:
    """Long-polling loop. `stop_check` is an awaitable returning True when
    the loop must exit (app shutdown)."""
    offset = 0
    token = ""
    while not await stop_check():
        new_token = await audit_mod.get_setting("telegram_bot_token")
        if new_token != token:
            token = new_token
            offset = 0
            if not token:
                await _sleep(stop_check, 5)
                continue
            log.info("Telegram bot started")
        if not token:
            await _sleep(stop_check, 10)
            continue
        try:
            data = await call(token, "getUpdates", offset=offset, timeout=25)
        except Exception:
            await _sleep(stop_check, 5)
            continue
        if not data.get("ok"):
            # Bad token (401) or Telegram hiccup — back off, re-read settings.
            await _sleep(stop_check, 15)
            continue
        for update in data.get("result", []):
            offset = update["update_id"] + 1
            message = update.get("message") or update.get("edited_message") or {}
            chat_id = message.get("chat", {}).get("id")
            text = (message.get("text") or "").strip()
            if chat_id and text.startswith("/"):
                try:
                    await handle_command(token, chat_id, text)
                except Exception as e:
                    log.warning("Telegram command failed: %s", e)
                    await notify_admin(f"⚠️ Ошибка команды: {e}")


async def _sleep(stop_check, seconds: float) -> None:
    import asyncio

    try:
        await asyncio.wait_for(stop_check(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
