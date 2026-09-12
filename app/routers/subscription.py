"""GET /sub/:id — public subscription endpoint consumed by the ForgeFox
desktop/Android clients (plain-text ssh:// lines, optionally base64)."""

from __future__ import annotations

import base64

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import PlainTextResponse
from sqlalchemy import select

from ..db import sessions
from ..models import Client, Node

router = APIRouter()


def _encode_fragment(s: str) -> str:
    # Percent-encode a URI fragment component (node names may have spaces)
    out = []
    safe = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
    for byte in s.encode("utf-8"):
        ch = chr(byte)
        out.append(ch if ch in safe else f"%{byte:02X}")
    return "".join(out)


@router.get("/sub/{client_id}", response_class=PlainTextResponse)
async def get_subscription(client_id: str, format: str = "plain"):
    async with sessions()() as db:
        client = await db.get(Client, client_id)
        if client is None:
            raise HTTPException(status_code=404, detail="Client not found")
        status = client.status or "active"
        if status in ("blocked", "limited", "expired"):
            # Keep the reason out of the body — the panel operator knows it.
            raise HTTPException(status_code=410, detail="Subscription is not active")
        rows = await db.execute(select(Node))
        nodes = {n.id: n for n in rows.scalars()}

    lines = []
    for node_id in client.all_node_ids():
        node = nodes.get(node_id)
        if node is None:
            continue
        name = f"{node.name} ({client.username})"
        lines.append(
            f"ssh://{client.username}:{client.password or 'generated_password_here'}"
            f"@{node.ip}:{node.port}#{_encode_fragment(name)}"
        )
    if not lines:
        raise HTTPException(status_code=404, detail="Node not found")

    body = "\n".join(lines) + "\n"
    if format == "base64":
        body = base64.b64encode(body.encode("utf-8")).decode("ascii")
    return Response(content=body, media_type="text/plain; charset=utf-8")
