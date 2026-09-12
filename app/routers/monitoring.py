"""GET /api/monitoring — real CPU/RAM/traffic per node via SSH (probes in
parallel; nodes that fail SSH report online=false)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends
from sqlalchemy import select

from .. import ssh
from ..db import sessions
from ..models import Node
from .deps import require_auth

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])


@router.get("/monitoring")
async def get_monitoring():
    async with sessions()() as db:
        rows = await db.execute(select(Node))
        nodes = list(rows.scalars())

    async def probe(node: Node) -> dict:
        try:
            return await ssh.probe_monitor(node)
        except ssh.NodeError:
            return ssh.offline_monitor(node.id)

    return await asyncio.gather(*(probe(n) for n in nodes))
