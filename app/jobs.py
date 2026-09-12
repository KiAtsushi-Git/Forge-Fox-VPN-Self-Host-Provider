"""Background jobs (plain asyncio tasks, no extra scheduler dependency):

  * node poller      — SSH probe every `node_poll_interval` seconds: updates
                       node online/offline status and records per-day network
                       traffic into node_stats (delta vs. last counters).
  * traffic collector— every `traffic_poll_interval` seconds reads the
                       iptables FF-IN/FF-OUT counters per user on each node,
                       updates clients.used_bytes + traffic_history, and
                       enforces limits/expiry (auto-block on the nodes).

Both loops re-read their interval from the settings table on every cycle, so
a Settings change applies without a restart.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from sqlalchemy import select

from . import audit as audit_mod, ssh
from .db import sessions
from .models import Client, Node, NodeStat, TrafficHistory, today_str
from .telegram_notify import notify_admin

log = logging.getLogger("forgefox.jobs")

_stop = asyncio.Event()


def request_stop() -> None:
    _stop.set()


async def _stopped() -> bool:
    return _stop.is_set()


async def _nap(seconds: float) -> None:
    with_context = asyncio.wait_for(_stopped.wait(), timeout=seconds)
    try:
        await with_context
    except asyncio.TimeoutError:
        pass


# ── node poller ─────────────────────────────────────────────────────────────

async def poll_nodes_once() -> None:
    async with sessions()() as db:
        nodes = list((await db.execute(select(Node))).scalars())

    async def probe(node: Node):
        try:
            mon = await ssh.probe_monitor(node)
            alive = True
        except ssh.NodeError:
            mon = None
            alive = False

        async with sessions()() as db:
            row = await db.get(Node, node.id)
            if row is None:
                return
            was_online = row.status == "online"
            row.status = "online" if alive else "offline"
            if mon:
                row.installed = "yes" if mon["vpn"]["installed"] else "no"
                row.sessions = ",".join(mon["sessions"])
                rx, tx = mon["rx_bytes"], mon["tx_bytes"]
                last_rx, last_tx = row.last_rx or 0, row.last_tx or 0
                # Counter went backwards -> node rebooted; take the current
                # value as this cycle's traffic.
                d_rx = rx - last_rx if rx >= last_rx else rx
                d_tx = tx - last_tx if tx >= last_tx else tx
                if row.last_rx is not None and (d_rx or d_tx):
                    stat = await db.get(NodeStat, (today_str(), node.id))
                    if stat is None:
                        stat = NodeStat(day=today_str(), node_id=node.id, rx_bytes=0, tx_bytes=0)
                        db.add(stat)
                    stat.rx_bytes += max(0, d_rx)
                    stat.tx_bytes += max(0, d_tx)
                row.last_rx, row.last_tx = rx, tx
            await db.commit()

        if not alive and was_online and await audit_mod.get_setting("notify_node_offline") == "1":
            await notify_admin(f"🔴 Нода «{node.name}» ({node.ip}) недоступна")

    await asyncio.gather(*(probe(n) for n in nodes), return_exceptions=True)


async def node_poller() -> None:
    log.info("Node poller started")
    while not _stop.is_set():
        try:
            await poll_nodes_once()
        except Exception as e:
            log.warning("node poll cycle failed: %s", e)
        interval = await audit_mod.get_int_setting("node_poll_interval", 60)
        await _nap(max(10, interval))


# ── traffic collector + enforcement ─────────────────────────────────────────

def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


async def collect_traffic_once() -> None:
    async with sessions()() as db:
        nodes = list((await db.execute(select(Node))).scalars())
        clients = list((await db.execute(select(Client))).scalars())

    by_username = {c.username: c for c in clients}
    actions: list[tuple[Client, str]] = []  # (client, new_status)

    for node in nodes:
        try:
            traffic = await ssh.probe_traffic(node)
        except ssh.NodeError as e:
            log.debug("traffic probe failed on %s: %s", node.name, e)
            continue

        async with sessions()() as db:
            for username, (rx, tx) in traffic.items():
                client = by_username.get(username)
                if client is None:
                    continue
                row = await db.get(Client, client.id)
                if row is None:
                    continue
                # delta since... we don't store per-user last counters, so
                # keep a module-level snapshot between cycles.
                key = f"{node.id}:{username}"
                prev = _last_user_traffic.get(key)
                _last_user_traffic[key] = (rx + tx)
                if prev is None:
                    continue  # first sighting: baseline, no delta
                total = rx + tx
                delta = total - prev if total >= prev else total  # reboot -> restart from 0
                if delta <= 0:
                    continue
                row.used_bytes = (row.used_bytes or 0) + delta
                hist = await db.get(TrafficHistory, (today_str(), row.id))
                if hist is None:
                    hist = TrafficHistory(day=today_str(), client_id=row.id, bytes=0)
                    db.add(hist)
                hist.bytes += delta
            await db.commit()

    # Enforcement: limits and expiry.
    async with sessions()() as db:
        clients = list((await db.execute(select(Client))).scalars())
    for client in clients:
        if (client.status or "active") != "active":
            continue
        limit_bytes = (client.limit_gb or 0) * 1024**3
        if limit_bytes and (client.used_bytes or 0) >= limit_bytes:
            actions.append((client, "limited"))
            continue
        expiry = _parse_ts(client.expiry)
        if expiry and expiry < datetime.now():
            actions.append((client, "expired"))

    for client, new_status in actions:
        nodes_now: list[Node]
        async with sessions()() as db:
            ids = client.all_node_ids()
            rows = await db.execute(select(Node).where(Node.id.in_(ids)))
            nodes_now = list(rows.scalars())
        for node in nodes_now:
            try:
                await ssh.block_user(node, client.username)
            except ssh.NodeError as e:
                log.warning("auto-block failed on %s: %s", node.name, e)
        async with sessions()() as db:
            row = await db.get(Client, client.id)
            if row:
                row.status = new_status
                await db.commit()
        await audit_mod.audit("system", f"Клиент {client.username} заблокирован: {new_status}")
        if new_status == "limited" and await audit_mod.get_setting("notify_limit") == "1":
            await notify_admin(f"⚠️ {client.username} превысил лимит трафика и заблокирован")
        elif new_status == "expired" and await audit_mod.get_setting("notify_expiry") == "1":
            await notify_admin(f"⌛ Подписка {client.username} истекла — пользователь заблокирован")


# (node_id, username) -> last (rx+tx) snapshot
_last_user_traffic: dict[str, int] = {}


async def expiry_notifier() -> None:
    """Once a day-ish: warn about subscriptions expiring within 3 days."""
    while not _stop.is_set():
        try:
            async with sessions()() as db:
                clients = list((await db.execute(select(Client))).scalars())
            now = datetime.now()
            for c in clients:
                if (c.status or "active") != "active":
                    continue
                expiry = _parse_ts(c.expiry)
                if not expiry:
                    continue
                hours_left = (expiry - now).total_seconds() / 3600
                if 0 < hours_left <= 72:
                    key = f"expiry_notified:{c.id}:{expiry.strftime('%Y%m%d%H')}"
                    if await audit_mod.get_setting(key) != "1":
                        await audit_mod.set_setting(key, "1")
                        if await audit_mod.get_setting("notify_expiry") == "1":
                            await notify_admin(
                                f"⌛ Подписка {c.username} истекает: {c.expiry}"
                            )
        except Exception as e:
            log.warning("expiry notifier failed: %s", e)
        await _nap(6 * 3600)  # check every 6h; notification is per-hour-keyed


async def traffic_collector() -> None:
    log.info("Traffic collector started")
    while not _stop.is_set():
        try:
            await collect_traffic_once()
        except Exception as e:
            log.warning("traffic collect cycle failed: %s", e)
        interval = await audit_mod.get_int_setting("traffic_poll_interval", 300)
        await _nap(max(30, interval))


async def start_all() -> list[asyncio.Task]:
    from .telegram import bot_loop

    return [
        asyncio.create_task(node_poller()),
        asyncio.create_task(traffic_collector()),
        asyncio.create_task(expiry_notifier()),
        asyncio.create_task(bot_loop(_stopped)),
    ]
