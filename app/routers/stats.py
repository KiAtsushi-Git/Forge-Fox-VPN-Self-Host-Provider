"""Dashboard counters + real traffic statistics (per-day node traffic from
the poller's history — replaces the old mock chart data)."""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from ..db import sessions
from ..models import Client, Node, NodeStat, TrafficHistory
from .deps import require_auth

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])


def _month_start() -> str:
    now = datetime.now()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d")


@router.get("/dashboard")
async def dashboard():
    async with sessions()() as db:
        nodes = list((await db.execute(select(Node))).scalars())
        clients = list((await db.execute(select(Client))).scalars())
        month_start = _month_start()
        rows = await db.execute(
            select(TrafficHistory.client_id, func.sum(TrafficHistory.bytes)).where(
                TrafficHistory.day >= month_start
            )
        )
        client_month_bytes = {cid: total for cid, total in rows.all()}
        # Clients created before this month carry used_bytes from before the
        # history table existed; for those, count everything (best effort),
        # otherwise just the daily deltas recorded since.
        total_bytes = 0
        for c in clients:
            history = client_month_bytes.get(c.id, 0)
            if c.created_at and c.created_at >= month_start:
                total_bytes += history
            else:
                total_bytes += history or (c.used_bytes or 0)
        month_traffic_gb = round(total_bytes / 1024**3, 2)

    active = sum(1 for c in clients if (c.status or "active") == "active")
    blocked = sum(
        1 for c in clients if c.status in ("blocked", "expired", "limited")
    )
    # Live VPN sessions = forgefox users with an open SSH session (from the
    # last poller cycle) + nodes missing the VPN stack, for the install badge.
    active_sessions = sum(
        len([u for u in (n.sessions or "").split(",") if u]) for n in nodes
    )
    nodes_needing_setup = sum(1 for n in nodes if n.installed == "no")
    return {
        "status": "ok",
        "nodes_count": len(nodes),
        "online_nodes": sum(1 for n in nodes if n.status == "online"),
        "nodes_needing_setup": nodes_needing_setup,
        "clients_count": len(clients),
        "active_clients": active,
        "blocked_clients": blocked,
        "active_sessions": active_sessions,
        "month_traffic_gb": month_traffic_gb,
    }


@router.get("/stats/traffic")
async def traffic_stats(days: int = 14):
    """Per-day network totals (rx/tx) across all nodes for the chart."""
    days = max(1, min(days, 90))
    since = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    async with sessions()() as db:
        rows = await db.execute(
            select(NodeStat.day, func.sum(NodeStat.rx_bytes), func.sum(NodeStat.tx_bytes))
            .where(NodeStat.day >= since)
            .group_by(NodeStat.day)
            .order_by(NodeStat.day)
        )
        by_day = {day: (rx or 0, tx or 0) for day, rx, tx in rows.all()}

    series = []
    for i in range(days):
        day = (datetime.now() - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        rx, tx = by_day.get(day, (0, 0))
        series.append(
            {
                "day": day,
                "rx_gb": round(rx / 1024**3, 3),
                "tx_gb": round(tx / 1024**3, 3),
                "total_gb": round((rx + tx) / 1024**3, 3),
            }
        )
    return series
