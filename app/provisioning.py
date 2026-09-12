"""Client provisioning logic shared by the API, bulk import and the bot."""

from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import select

from . import audit as audit_mod
from . import security
from . import ssh
from .db import sessions
from .models import Client, Node, now_str

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def validate_username(username: str) -> str:
    username = username.strip()
    if not _USERNAME_RE.match(username):
        raise ValueError("Имя пользователя: 1-32 символа, только латиница, цифры, - и _")
    return username


def normalize_node_ids(node_id: str | None, node_ids: list[str] | None) -> list[str]:
    wanted: list[str] = list(node_ids or [])
    if node_id and node_id not in wanted:
        wanted.append(node_id)
    wanted = [w for w in wanted if w]
    seen: list[str] = []
    for w in wanted:
        if w not in seen:
            seen.append(w)
    if not seen:
        raise ValueError("Выберите хотя бы одну ноду")
    return seen


async def load_nodes(node_ids: list[str]) -> list[Node]:
    async with sessions()() as db:
        rows = await db.execute(select(Node).where(Node.id.in_(node_ids)))
        nodes = {n.id: n for n in rows.scalars()}
    missing = [nid for nid in node_ids if nid not in nodes]
    if missing:
        raise ValueError("Нода не найдена")
    return [nodes[nid] for nid in node_ids]


async def username_exists(username: str) -> bool:
    async with sessions()() as db:
        rows = await db.execute(select(Client).where(Client.username == username))
        return rows.scalars().first() is not None


async def create_client(
    actor: str,
    username: str,
    node_ids: list[str],
    expiry: str | None = None,
    limit_gb: int | None = None,
) -> Client:
    """Create a client: provision the SSH user on every node, then store the
    row. Rolls back created system users if a node fails, so a retry starts
    clean. Raises ValueError with a human-readable message."""
    username = validate_username(username)
    if await username_exists(username):
        raise ValueError(f"Пользователь «{username}» уже существует")

    expiry_dt = parse_expiry_or_raise(expiry)
    nodes = await load_nodes(node_ids)

    password = security.gen_password(20)
    created_on: list[Node] = []
    for node in nodes:
        try:
            await ssh.provision_user(node, username, password)
        except ssh.NodeError as e:
            for done in created_on:
                try:
                    await ssh.delete_user(done, username)
                except ssh.NodeError:
                    pass
            raise ValueError(f"Не удалось создать пользователя на ноде {node.name}: {e}")
        created_on.append(node)

    node_ids_csv = ",".join(n.id for n in nodes)
    client = Client(
        id=security.new_id(),
        username=username,
        node_id=nodes[0].id,
        node_ids=node_ids_csv,
        password=password,
        expiry=expiry_dt.strftime("%Y-%m-%d %H:%M:%S") if expiry_dt else None,
        limit_gb=int(limit_gb) if limit_gb else None,
        used_bytes=0,
        created_at=now_str(),
        status="active",
    )
    async with sessions()() as db:
        db.add(client)
        await db.commit()
    await audit_mod.audit(actor, f"Создан клиент {username} (ноды: {node_ids_csv})")
    return client


def parse_expiry_or_raise(expiry: str | None) -> datetime | None:
    from .routers.deps import parse_expiry

    return parse_expiry(expiry)


async def remove_client_from_nodes(client: Client, nodes: list[Node]) -> None:
    for node in nodes:
        try:
            await ssh.delete_user(node, client.username)
        except ssh.NodeError as e:
            ssh.log.warning("Could not delete user %s on node %s: %s", client.username, node.name, e)


async def block_client_on_nodes(client: Client, nodes: list[Node], status: str) -> None:
    for node in nodes:
        try:
            await ssh.block_user(node, client.username)
        except ssh.NodeError as e:
            ssh.log.warning("Could not block %s on node %s: %s", client.username, node.name, e)
    async with sessions()() as db:
        row = await db.get(Client, client.id)
        if row:
            row.status = status
            await db.commit()
