"""Shared FastAPI dependencies."""

from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException, Request

from ..db import sessions
from ..models import Session
from ..security import session_expired

_EXPIRY_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M")


async def require_auth(request: Request) -> str:
    """Require a valid `Authorization: Bearer <token>`; returns the username."""
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized")
    async with sessions()() as db:
        row = await db.get(Session, token)
        if row is None or session_expired(row.expires_at):
            raise HTTPException(status_code=401, detail="Unauthorized")
        return row.username


def parse_expiry(raw: str | None) -> datetime | None:
    """Parse the expiry formats the dashboard can produce; empty = unlimited."""
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in _EXPIRY_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise ValueError(f"Не удалось разобрать дату: {raw} (ожидается ГГГГ-ММ-ДД ЧЧ:ММ)")
