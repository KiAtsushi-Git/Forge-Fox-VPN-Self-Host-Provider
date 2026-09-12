"""Panel settings, admin password change, DB backup and stats reset —
the endpoints the Settings tab buttons were never wired to."""

from __future__ import annotations

import io
import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import delete, select, text, update

from .. import audit as audit_mod
from ..db import engine, is_sqlite, sessions
from ..models import Admin, AuditLog, Client, NodeStat, Setting, TrafficHistory
from ..schemas import PasswordChange, SettingsUpdate
from ..security import hash_password, verify_password
from .deps import require_auth

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])
log = logging.getLogger("forgefox.settings")

BOOL_KEYS = ("notify_expiry", "notify_limit", "notify_node_offline")
STR_KEYS = ("telegram_bot_token", "telegram_admin_chat_id", "panel_url")
INT_KEYS = ("node_poll_interval", "traffic_poll_interval")


async def current_admin(username: str) -> Admin | None:
    async with sessions()() as db:
        rows = await db.execute(select(Admin).where(Admin.username == username))
        return rows.scalars().first()


@router.get("/settings")
async def get_settings():
    return await audit_mod.get_all_settings()


@router.put("/settings")
async def put_settings(payload: SettingsUpdate, user: str = Depends(require_auth)):
    data = payload.model_dump(exclude_none=True)
    for key, value in data.items():
        if key in BOOL_KEYS:
            value = "1" if value else "0"
        elif key in INT_KEYS:
            try:
                value = str(max(10, int(value)))  # floor: 10s
            except (TypeError, ValueError):
                continue
        elif key not in STR_KEYS:
            continue
        await audit_mod.set_setting(key, str(value))
    await audit_mod.audit(user, "Настройки панели обновлены")
    return await audit_mod.get_all_settings()


@router.post("/settings/password")
async def change_password(payload: PasswordChange, user: str = Depends(require_auth)):
    if len(payload.new_password) < 6:
        raise HTTPException(status_code=400, detail="Новый пароль: минимум 6 символов")
    admin = await current_admin(user)
    if admin is None or not verify_password(payload.old_password, admin.password_hash):
        raise HTTPException(status_code=401, detail="Текущий пароль неверен")
    async with sessions()() as db:
        row = await db.get(Admin, admin.id)
        row.password_hash = hash_password(payload.new_password)
        await db.commit()
    await audit_mod.audit(user, "Пароль администратора изменён")
    return {"status": "ok", "message": "Пароль изменён"}


@router.get("/backup")
async def download_backup(user: str = Depends(require_auth)):
    """Download a consistent snapshot of the database.

    SQLite: the online-backup API (safe while the panel keeps writing).
    PostgreSQL: a plain SQL dump of every table (no pg_dump needed in the
    container — it isn't installed there).
    """
    await audit_mod.audit(user, "Скачан бэкап базы данных")
    if is_sqlite():
        import sqlite3

        from .. import config

        url, _ = config.sqlalchemy_url()
        path = url.split("///", 1)[1]
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        tmp.close()
        src = sqlite3.connect(path)
        dst = sqlite3.connect(tmp.name)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        data = Path(tmp.name).read_bytes()
        Path(tmp.name).unlink(missing_ok=True)
        return Response(
            content=data,
            media_type="application/octet-stream",
            headers={"Content-Disposition": "attachment; filename=forgefox-backup.db"},
        )

    # PostgreSQL dump
    eng = engine()
    async with eng.connect() as conn:
        tables = ("admins", "nodes", "clients", "audit_log", "sessions", "settings",
                  "traffic_history", "node_stats")
        out = io.StringIO()
        out.write("-- ForgeFox Provider PostgreSQL backup\n")
        for table in tables:
            try:
                rows = await conn.execute(text(f'SELECT * FROM "{table}"'))
            except Exception:
                continue
            for row in rows.fetchall():
                values = ", ".join(
                    "NULL" if v is None else "'" + str(v).replace("'", "''") + "'"
                    for v in row
                )
                out.write(f"INSERT INTO {table} VALUES ({values});\n")
        return Response(
            content=out.getvalue(),
            media_type="application/sql",
            headers={"Content-Disposition": "attachment; filename=forgefox-backup.sql"},
        )


@router.post("/stats/reset")
async def reset_stats(user: str = Depends(require_auth)):
    async with sessions()() as db:
        await db.execute(update(Client).values(used_bytes=0))
        await db.execute(delete(TrafficHistory))
        await db.execute(delete(NodeStat))
        await db.commit()
    await audit_mod.audit(user, "Статистика трафика сброшена")
    return {"status": "ok", "message": "Статистика сброшена"}


@router.post("/settings/telegram-test")
async def telegram_test(user: str = Depends(require_auth)):
    """Send a test message to the configured admin chat."""
    from ..telegram_notify import notify_admin

    token = await audit_mod.get_setting("telegram_bot_token")
    chat_id = await audit_mod.get_setting("telegram_admin_chat_id")
    if not token:
        raise HTTPException(status_code=400, detail="Сначала сохраните Bot Token")
    if not chat_id:
        raise HTTPException(
            status_code=400,
            detail="Chat ID не задан — напишите боту /start в Telegram, он привяжет чат",
        )
    await notify_admin("✅ ForgeFox: тестовое сообщение — бот работает")
    await audit_mod.audit(user, "Отправлен тест Telegram-уведомления")
    return {"status": "ok", "message": "Тестовое сообщение отправлено"}


@router.get("/logs")
async def get_logs():
    async with sessions()() as db:
        rows = await db.execute(
            select(AuditLog).order_by(AuditLog.time.desc()).limit(500)
        )
        return [
            {"time": row.time, "user": row.user or "system", "action": row.action}
            for row in rows.scalars()
        ]
