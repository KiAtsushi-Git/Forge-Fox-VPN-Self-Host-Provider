"""POST /api/login — issue a session token for valid credentials."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from sqlalchemy import delete, select

from .. import audit as audit_mod
from ..db import sessions
from ..models import Admin, Session
from ..schemas import LoginRequest
from ..security import session_expiry_str, session_expired, new_token, verify_password

router = APIRouter()
log = logging.getLogger("forgefox.auth")


@router.post("/api/login")
async def login(payload: LoginRequest):
    async with sessions()() as db:
        row = await db.execute(select(Admin).where(Admin.username == payload.username))
        admin = row.scalars().first()
        valid = admin is not None and verify_password(payload.password, admin.password_hash)

    if not valid:
        log.warning("Failed login attempt for user '%s'", payload.username)
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")

    token = new_token()
    async with sessions()() as db:
        # Housekeeping: drop expired sessions while we're here.
        rows = await db.execute(select(Session))
        for s in rows.scalars():
            if session_expired(s.expires_at):
                await db.delete(s)
        db.add(Session(token=token, username=payload.username, expires_at=session_expiry_str()))
        await db.commit()

    log.info("Admin '%s' logged in", payload.username)
    return {"token": token, "username": payload.username}


async def purge_expired_sessions() -> None:
    async with sessions()() as db:
        rows = await db.execute(select(Session))
        for s in rows.scalars():
            if session_expired(s.expires_at):
                await db.delete(s)
        await db.commit()
