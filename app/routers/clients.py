"""Client (VPN user) management: create/bulk/edit/block/delete, password
reset, export/import and QR codes for the subscription link."""

from __future__ import annotations

import io
import logging
from datetime import datetime, timedelta

import segno
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select

from .. import audit as audit_mod
from .. import provisioning, ssh
from ..db import sessions
from ..models import Client, Node, TrafficHistory
from ..schemas import BulkNewClients, ClientUpdate, ImportClients, NewClient
from ..security import gen_password
from .deps import parse_expiry, require_auth

router = APIRouter(prefix="/api/clients", dependencies=[Depends(require_auth)])
log = logging.getLogger("forgefox.clients")

_VALID_STATUSES = ("active", "blocked", "expired", "limited")


async def get_client_or_404(client_id: str) -> Client:
    async with sessions()() as db:
        client = await db.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Клиент не найден")
    return client


async def nodes_for_client(client: Client) -> list[Node]:
    ids = client.all_node_ids()
    async with sessions()() as db:
        rows = await db.execute(select(Node).where(Node.id.in_(ids)))
        return list(rows.scalars())


@router.get("")
async def list_clients():
    async with sessions()() as db:
        rows = await db.execute(select(Client).order_by(Client.created_at))
        return [c.to_dict() for c in rows.scalars()]


@router.post("", status_code=201)
async def add_client(payload: NewClient, user: str = Depends(require_auth)):
    try:
        node_ids = provisioning.normalize_node_ids(payload.node_id, payload.node_ids)
        parse_expiry(payload.expiry)  # validate before touching any node
        client = await provisioning.create_client(
            user, payload.username, node_ids, payload.expiry, payload.limit_gb
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return client.to_dict()


@router.post("/bulk", status_code=201)
async def bulk_add_clients(payload: BulkNewClients, user: str = Depends(require_auth)):
    """Create many users at once (one username per row in the UI). Partial
    success is reported per username so one bad name doesn't kill the batch."""
    try:
        parse_expiry(payload.expiry)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    created, failed = [], []
    for raw in payload.usernames:
        username = raw.strip()
        if not username:
            continue
        try:
            client = await provisioning.create_client(
                user, username, payload.node_ids, payload.expiry, payload.limit_gb
            )
            created.append(client.to_dict())
        except ValueError as e:
            failed.append({"username": username, "error": str(e)})
    return {"created": created, "failed": failed}


@router.put("/{client_id}")
async def update_client(client_id: str, payload: ClientUpdate, user: str = Depends(require_auth)):
    client = await get_client_or_404(client_id)

    try:
        # None = don't touch; "" = unlimited; otherwise parse
        expiry = parse_expiry(payload.expiry) if payload.expiry is not None else None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    new_node_ids = payload.node_ids
    if new_node_ids is not None:
        new_node_ids = [n for n in new_node_ids if n]
        if not new_node_ids:
            raise HTTPException(status_code=400, detail="Список нод не может быть пустым")

    async with sessions()() as db:
        row = await db.get(Client, client_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Клиент не найден")

        old_node_ids = row.all_node_ids()
        if new_node_ids is not None:
            added = [n for n in new_node_ids if n not in old_node_ids]
            removed = [n for n in old_node_ids if n not in new_node_ids]
            if added:
                nodes = await provisioning.load_nodes(added)
                for node in nodes:
                    await ssh.provision_user(node, row.username, row.password or gen_password())
            if removed:
                rows = await db.execute(select(Node).where(Node.id.in_(removed)))
                for node in rows.scalars():
                    try:
                        await ssh.delete_user(node, row.username)
                    except ssh.NodeError as e:
                        log.warning("delete_user failed on %s: %s", node.name, e)
            row.node_ids = ",".join(new_node_ids)
            row.node_id = new_node_ids[0]

        if payload.expiry is not None:
            row.expiry = expiry.strftime("%Y-%m-%d %H:%M:%S") if expiry else None
        if payload.limit_gb is not None:
            row.limit_gb = int(payload.limit_gb) if payload.limit_gb > 0 else None
        if payload.status is not None:
            if payload.status not in _VALID_STATUSES:
                raise HTTPException(status_code=400, detail=f"Статус должен быть одним из {_VALID_STATUSES}")
            nodes = await nodes_for_client(row)
            if payload.status == "active":
                for node in nodes:
                    await ssh.unblock_user(node, row.username)
            else:
                for node in nodes:
                    await ssh.block_user(node, row.username)
            row.status = payload.status
        await db.commit()
        result = row.to_dict()

    await audit_mod.audit(user, f"Клиент {result['username']} обновлён")
    return result


@router.post("/{client_id}/reset-password")
async def reset_password(client_id: str, user: str = Depends(require_auth)):
    client = await get_client_or_404(client_id)
    nodes = await nodes_for_client(client)
    if not nodes:
        raise HTTPException(status_code=400, detail="Ноды клиента не найдены")
    new_pass = gen_password(20)
    try:
        for node in nodes:
            await ssh.change_password(node, client.username, new_pass)
    except ssh.NodeError as e:
        raise HTTPException(status_code=502, detail=f"Не удалось сменить пароль: {e}")

    async with sessions()() as db:
        row = await db.get(Client, client_id)
        row.password = new_pass
        if row.status in ("limited", "expired"):
            row.status = "active"
        await db.commit()

    await audit_mod.audit(user, f"Клиенту {client.username} сброшен пароль")
    return {"id": client_id, "password": new_pass, "message": "Пароль обновлён — ссылка-подписка действительна"}


@router.post("/{client_id}/block")
async def block_client(client_id: str, user: str = Depends(require_auth)):
    client = await get_client_or_404(client_id)
    nodes = await nodes_for_client(client)
    for node in nodes:
        try:
            await ssh.block_user(node, client.username)
        except ssh.NodeError as e:
            log.warning("block_user failed on %s: %s", node.name, e)
    async with sessions()() as db:
        row = await db.get(Client, client_id)
        row.status = "blocked"
        await db.commit()
    await audit_mod.audit(user, f"Клиент {client.username} заблокирован")
    return {"id": client_id, "status": "blocked"}


@router.post("/{client_id}/unblock")
async def unblock_client(client_id: str, user: str = Depends(require_auth)):
    client = await get_client_or_404(client_id)
    nodes = await nodes_for_client(client)
    for node in nodes:
        try:
            await ssh.unblock_user(node, client.username)
        except ssh.NodeError as e:
            log.warning("unblock_user failed on %s: %s", node.name, e)
    async with sessions()() as db:
        row = await db.get(Client, client_id)
        row.status = "active"
        await db.commit()
    await audit_mod.audit(user, f"Клиент {client.username} разблокирован")
    return {"id": client_id, "status": "active"}


@router.delete("/{client_id}", status_code=204)
async def delete_client(client_id: str, user: str = Depends(require_auth)):
    client = await get_client_or_404(client_id)
    nodes = await nodes_for_client(client)
    for node in nodes:
        try:
            await ssh.delete_user(node, client.username)
        except ssh.NodeError:
            pass
    async with sessions()() as db:
        row = await db.get(Client, client_id)
        await db.delete(row)
        await db.commit()
    await audit_mod.audit(user, f"Клиент {client.username} удалён")
    return None


@router.get("/export")
async def export_clients():
    """JSON dump of every client (without node SSH secrets) — for backups
    and for moving users to another panel."""
    async with sessions()() as db:
        rows = await db.execute(select(Client).order_by(Client.created_at))
        data = [
            {
                "username": c.username,
                "password": c.password or "",
                "node_ids": c.all_node_ids(),
                "expiry": c.expiry or "",
                "limit_gb": c.limit_gb,
                "used_bytes": c.used_bytes or 0,
                "status": c.status or "active",
                "created_at": c.created_at or "",
            }
            for c in rows.scalars()
        ]
    import json

    body = json.dumps({"clients": data}, ensure_ascii=False, indent=2)
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=forgefox-clients.json"},
    )


@router.post("/import", status_code=201)
async def import_clients(payload: ImportClients, user: str = Depends(require_auth)):
    """Re-create clients from an export dump. Existing usernames are skipped."""
    created, skipped, failed = [], [], []
    for item in payload.clients:
        username = (item.get("username") or "").strip()
        if not username:
            continue
        if await provisioning.username_exists(username):
            skipped.append(username)
            continue
        try:
            client = await provisioning.create_client(
                user,
                username,
                item.get("node_ids") or [item.get("node_id")],
                item.get("expiry") or None,
                item.get("limit_gb"),
            )
            created.append(client.to_dict())
        except ValueError as e:
            failed.append({"username": username, "error": str(e)})
    return {"created": len(created), "skipped": skipped, "failed": failed}


@router.get("/{client_id}/traffic")
async def client_traffic(client_id: str, days: int = 14):
    """Per-day traffic for one client (the detail chart in the UI)."""
    await get_client_or_404(client_id)
    days = max(1, min(days, 90))
    since = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    async with sessions()() as db:
        rows = await db.execute(
            select(TrafficHistory).where(
                TrafficHistory.client_id == client_id, TrafficHistory.day >= since
            )
        )
        by_day = {h.day: h.bytes for h in rows.scalars()}
    series = []
    for i in range(days):
        day = (datetime.now() - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        series.append({"day": day, "gb": round(by_day.get(day, 0) / 1024**3, 3)})
    return series


@router.get("/{client_id}/qr")
async def client_qr(client_id: str, host: str = ""):
    """PNG QR code of the subscription URL, generated locally (no external
    service). The browser knows the panel's public origin, so it passes it
    as ?host= — from inside the container the URL is not derivable."""
    client = await get_client_or_404(client_id)
    if not host:
        raise HTTPException(status_code=400, detail="Укажите ?host=<origin панели>")
    host = host.rstrip("/")
    if not host.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="host должен начинаться с http:// или https://")
    url = f"{host}/sub/{client.id}"
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="png", scale=8, border=2)
    return Response(content=buf.getvalue(), media_type="image/png")
