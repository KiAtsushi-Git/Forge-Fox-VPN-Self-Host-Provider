"""Node management: CRUD, SSH health check, host install (install.sh)."""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from .. import audit as audit_mod
from .. import ssh
from ..db import sessions
from ..models import Client, Node, now_str
from ..schemas import NewNode, NodeUpdate
from ..security import new_id
from .deps import require_auth

router = APIRouter(prefix="/api/nodes", dependencies=[Depends(require_auth)])
log = logging.getLogger("forgefox.nodes")

NODE_FIELDS = ("id", "name", "ip", "port", "ssh_user", "status", "created_at",
               "country", "installed", "sessions")


def node_dict(n: Node) -> dict:
    # ssh_pass is deliberately not exposed to the UI
    return {f: getattr(n, f) for f in NODE_FIELDS}


async def get_node_or_404(node_id: str) -> Node:
    async with sessions()() as db:
        node = await db.get(Node, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Нода не найдена")
    return node


async def lookup_country(ip: str) -> str:
    """Best-effort geo tag for the node (2-letter country code)."""
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"https://ipwho.is/{ip}")
            data = resp.json()
        if data.get("success"):
            return (data.get("country_code") or "").upper()
    except Exception:
        pass
    return ""


@router.get("")
async def list_nodes():
    async with sessions()() as db:
        rows = await db.execute(select(Node).order_by(Node.created_at))
        return [node_dict(n) for n in rows.scalars()]


@router.post("", status_code=201)
async def add_node(payload: NewNode, user: str = Depends(require_auth)):
    name = payload.name.strip()
    ip = payload.ip.strip()
    if not name or not ip:
        raise HTTPException(status_code=400, detail="Укажите название и IP ноды")
    port = payload.port if payload.port and 0 < payload.port <= 65535 else 22
    ssh_user = (payload.ssh_user or "").strip() or "root"

    node = Node(
        id=new_id(),
        name=name,
        ip=ip,
        port=port,
        ssh_user=ssh_user,
        ssh_pass=payload.ssh_pass,
        status="online",
        created_at=now_str(),
        country=await lookup_country(ip),
        installed="unknown",
    )

    # Probe the node right away: SSH reachable? VPN stack already installed?
    # A node that was set up before (desktop client's Host install, another
    # panel) must not demand a reinstall — and a node without the stack gets
    # a red "not installed" badge and the install button in the UI.
    vpn = None
    try:
        mon = await ssh.probe_monitor(node)
        node.status = "online"
        node.sessions = ",".join(mon["sessions"])
        vpn = mon["vpn"]
        node.installed = "yes" if vpn["installed"] else "no"
    except ssh.NodeError:
        node.status = "offline"

    async with sessions()() as db:
        db.add(node)
        await db.commit()
    await audit_mod.audit(user, f"Добавлена нода {name} ({ip}:{port})")
    return {**node_dict(node), "vpn": vpn or {"installed": None, "known": False}}


@router.put("/{node_id}")
async def update_node(node_id: str, payload: NodeUpdate, user: str = Depends(require_auth)):
    if payload.port is not None and not (1 <= payload.port <= 65535):
        raise HTTPException(status_code=400, detail="Порт должен быть 1-65535")

    async with sessions()() as db:
        node = await db.get(Node, node_id)
        if node is None:
            raise HTTPException(status_code=404, detail="Нода не найдена")
        if payload.name is not None and payload.name.strip():
            node.name = payload.name.strip()
        if payload.ip is not None and payload.ip.strip():
            node.ip = payload.ip.strip()
        if payload.port is not None:
            node.port = payload.port
        if payload.ssh_user is not None and payload.ssh_user.strip():
            node.ssh_user = payload.ssh_user.strip()
        if payload.ssh_pass:  # empty string = keep current
            node.ssh_pass = payload.ssh_pass
        await db.commit()
        result = node_dict(node)

    await audit_mod.audit(user, f"Нода {node_id} обновлена")
    return result


@router.delete("/{node_id}", status_code=204)
async def delete_node(node_id: str, user: str = Depends(require_auth)):
    async with sessions()() as db:
        node = await db.get(Node, node_id)
        if node is None:
            raise HTTPException(status_code=404, detail="Нода не найдена")

        # Clients that live ONLY on this node go away entirely (system users
        # removed best-effort); multi-node clients just lose this node.
        rows = await db.execute(select(Client))
        clients = list(rows.scalars())
        for c in clients:
            ids = c.all_node_ids()
            if node_id in ids:
                remaining = [i for i in ids if i != node_id]
                if remaining:
                    c.node_ids = ",".join(remaining)
                    c.node_id = remaining[0]
                else:
                    await db.delete(c)
        await db.delete(node)
        await db.commit()

    # Best-effort: remove the system users belonging to the deleted clients.
    orphaned = [c for c in clients if node_id in c.all_node_ids() and len(c.all_node_ids()) == 1]
    for c in orphaned:
        try:
            await ssh.delete_user(node, c.username)
        except ssh.NodeError:
            pass

    await audit_mod.audit(user, f"Нода {node.name} ({node.ip}) удалена")
    return None


@router.post("/{node_id}/check")
async def check_node(node_id: str, user: str = Depends(require_auth)):
    node = await get_node_or_404(node_id)
    status = "online" if await ssh.check_alive(node) else "offline"
    installed = node.installed or "unknown"
    sessions: list[str] = []
    if status == "online":
        # Full probe: refresh the install status and live sessions too.
        try:
            mon = await ssh.probe_monitor(node)
            installed = "yes" if mon["vpn"]["installed"] else "no"
            sessions = mon["sessions"]
        except ssh.NodeError:
            pass
    async with sessions()() as db:
        row = await db.get(Node, node_id)
        if row:
            row.status = status
            row.installed = installed
            row.sessions = ",".join(sessions)
            await db.commit()
    return {"id": node_id, "status": status, "installed": installed, "sessions": sessions}


@router.get("/{node_id}/detail")
async def node_detail(node_id: str):
    """Everything about one node in one place: live probe (CPU/RAM/disk/
    sessions/install breakdown), the clients living on it and their usage."""
    node = await get_node_or_404(node_id)
    try:
        mon = await ssh.probe_monitor(node)
    except ssh.NodeError as e:
        mon = ssh.offline_monitor(node.id)
        mon["error"] = str(e)

    async with sessions()() as db:
        rows = await db.execute(select(Client))
        clients = [c.to_dict() for c in rows.scalars() if node_id in c.all_node_ids()]

    online_usernames = set(mon["sessions"])
    for c in clients:
        c["online_now"] = c["username"] in online_usernames

    return {
        "node": node_dict(node),
        "monitor": mon,
        "clients": clients,
    }


@router.post("/{node_id}/reboot")
async def reboot_node(node_id: str, user: str = Depends(require_auth)):
    """Reboot the node (the UI double-confirms this)."""
    node = await get_node_or_404(node_id)
    await audit_mod.audit(user, f"Перезагрузка ноды {node.name} ({node.ip})")
    try:
        # `reboot` needs a moment; don't wait for the channel to close.
        await ssh.run_node_command(node, "nohup reboot >/dev/null 2>&1 &", timeout=10)
    except ssh.NodeError as e:
        raise HTTPException(status_code=502, detail=f"Не удалось отправить reboot: {e}")
    async with sessions()() as db:
        row = await db.get(Node, node_id)
        if row:
            row.status = "offline"
            row.sessions = None
            await db.commit()
    return {"status": "ok", "message": "Нода перезагружается — статус обновится автоматически"}


@router.post("/{node_id}/setup")
async def run_node_setup(node_id: str, user: str = Depends(require_auth)):
    node = await get_node_or_404(node_id)
    await audit_mod.audit(user, f"Настройка ноды {node.name} ({node.ip}): запуск install.sh")
    try:
        output = await ssh.setup_node(node)
        status = "online"
    except ssh.NodeError as e:
        log.error("Node %s setup failed: %s", node.name, e)
        output = ""
        status = "offline"
        error = str(e)

    async with sessions()() as db:
        row = await db.get(Node, node_id)
        if row:
            row.status = status
            if status == "online":
                row.installed = "yes"  # setup just installed everything
            await db.commit()

    if status == "online":
        tail = "\n".join(output.splitlines()[-30:])
        log.info("Node %s setup complete:\n%s", node.name, tail)
        return {"id": node_id, "status": "online", "message": "Нода настроена — VPN-стек установлен"}
    raise HTTPException(status_code=502, detail=f"Не удалось настроить ноду: {error}")
