"""SQLAlchemy models.

The tables match the Rust panel's schema exactly (same names, same columns,
same TEXT-typed timestamps) so an existing forgefox.db / PostgreSQL database
keeps working untouched. New columns and tables arrive through the migration
files — the ORM marks them nullable/optional so old rows are still readable.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now_str() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def today_str() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")


class Base(DeclarativeBase):
    pass


class Admin(Base):
    __tablename__ = "admins"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    username: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str | None] = mapped_column(Text)


class Node(Base):
    __tablename__ = "nodes"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    ip: Mapped[str] = mapped_column(Text, nullable=False)
    port: Mapped[int] = mapped_column(Integer, default=22)
    ssh_user: Mapped[str] = mapped_column(Text, default="root")
    ssh_pass: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text, default="offline")
    created_at: Mapped[str | None] = mapped_column(Text)
    # new (python panel): best-effort geo tag and /proc/net/dev counters
    # from the last poll — used for per-day node traffic deltas.
    country: Mapped[str | None] = mapped_column(Text)
    last_rx: Mapped[int | None] = mapped_column(BigInteger)
    last_tx: Mapped[int | None] = mapped_column(BigInteger)
    # VPN stack presence: 'unknown' (not probed yet) / 'yes' / 'no' —
    # whether forgefox-bridge is installed on the node.
    installed: Mapped[str | None] = mapped_column(Text)
    # Live VPN users (forgefox-group members with an active SSH session),
    # comma-separated, from the last poll — feeds the dashboard counter.
    sessions: Mapped[str | None] = mapped_column(Text)


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    username: Mapped[str] = mapped_column(Text, nullable=False)
    # legacy single-node column (kept for old client builds) + full list
    node_id: Mapped[str] = mapped_column(Text, nullable=False)
    node_ids: Mapped[str | None] = mapped_column(Text)
    password: Mapped[str | None] = mapped_column(Text)
    expiry: Mapped[str | None] = mapped_column(Text)
    limit_gb: Mapped[int | None] = mapped_column(Integer)
    used_bytes: Mapped[int | None] = mapped_column(BigInteger, default=0)
    created_at: Mapped[str | None] = mapped_column(Text)
    # new (python panel): active / blocked / expired / limited
    status: Mapped[str | None] = mapped_column(Text, default="active")

    def all_node_ids(self) -> list[str]:
        raw = self.node_ids or ""
        ids = [s.strip() for s in raw.split(",") if s.strip()]
        if not ids and self.node_id:
            ids = [self.node_id]
        return ids

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "node_id": self.node_id,
            "node_ids": self.node_ids or "",
            "expiry": self.expiry or "",
            "limit_gb": self.limit_gb,
            "used_bytes": self.used_bytes or 0,
            "created_at": self.created_at or "",
            "status": self.status or "active",
        }


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    time: Mapped[str] = mapped_column(Text, nullable=False)
    user: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text, nullable=False)


class Session(Base):
    __tablename__ = "sessions"

    token: Mapped[str] = mapped_column(Text, primary_key=True)
    username: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[str] = mapped_column(Text, nullable=False)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)


class TrafficHistory(Base):
    """Per-client per-day traffic (bytes), written by the traffic collector."""

    __tablename__ = "traffic_history"

    day: Mapped[str] = mapped_column(Text, primary_key=True)
    client_id: Mapped[str] = mapped_column(Text, primary_key=True)
    bytes: Mapped[int] = mapped_column(BigInteger, default=0)


class NodeStat(Base):
    """Per-node per-day network counters (bytes), written by the poller."""

    __tablename__ = "node_stats"

    day: Mapped[str] = mapped_column(Text, primary_key=True)
    node_id: Mapped[str] = mapped_column(Text, primary_key=True)
    rx_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    tx_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
