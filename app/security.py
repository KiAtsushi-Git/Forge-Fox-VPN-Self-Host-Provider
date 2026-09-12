"""Passwords, tokens and session helpers."""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta

import bcrypt

from . import config

# Unambiguous alphabet: no 0/O/1/l/I — the password is typed by humans
# into VPN clients.
_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"


def gen_password(length: int = 20) -> str:
    return "".join(secrets.choice(_CHARS) for _ in range(length))


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=10)).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except ValueError:
        return False


def new_token() -> str:
    return str(uuid.uuid4())


def new_id() -> str:
    return str(uuid.uuid4())


def session_expiry_str() -> str:
    expires = datetime.now() + timedelta(seconds=config.SESSION_TTL_SECONDS)
    return expires.strftime("%Y-%m-%d %H:%M:%S")


def session_expired(expires_at: str) -> bool:
    try:
        return datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S") <= datetime.now()
    except ValueError:
        return True
