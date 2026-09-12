"""Everything that talks to Edge Nodes over SSH (asyncssh).

The panel connects to each node with the credentials stored in the `nodes`
table and drives it with small shell snippets — the same approach the Rust
panel used, now with iptables accounting chains added per VPN user so
traffic can actually be measured (all tunnelled payload of a user flows
through their own sshd connection, which the `owner` match can count).
"""

from __future__ import annotations

import asyncio
import logging
import time

import asyncssh

from . import config
from .models import Node

log = logging.getLogger("forgefox.ssh")

# Per-node last traffic snapshot for rate calculation (node_id -> (ts, rx, tx))
_last_net: dict[str, tuple[float, int, int]] = {}


class NodeError(Exception):
    pass


async def _connect(node: Node) -> asyncssh.SSHClientConnection:
    if not node.ssh_pass:
        raise NodeError("у ноды не задан SSH пароль")
    try:
        return await asyncssh.connect(
            node.ip,
            port=int(node.port or 22),
            username=node.ssh_user or "root",
            password=node.ssh_pass,
            known_hosts=None,  # TODO: pin host keys per node
            connect_timeout=15,
        )
    except asyncssh.PermissionDenied:
        raise NodeError("SSH auth: неверный логин/пароль ноды")
    except (OSError, asyncssh.Error) as e:
        raise NodeError(f"SSH connect: {e}")


async def run_node_command(node: Node, command: str, timeout: int = config.SSH_PROVISION_TIMEOUT) -> str:
    """Run a command on the node, return its stdout (empty string on failure
    if check=False — use run_checked when the exit code matters)."""
    async with await _connect(node) as conn:
        result = await asyncio.wait_for(
            conn.run(command, check=False),
            timeout=timeout,
        )
        return result.stdout or ""


async def run_checked(node: Node, command: str, timeout: int = config.SSH_PROVISION_TIMEOUT) -> tuple[int, str]:
    """Run a command, return (exit_code, combined output)."""
    async with await _connect(node) as conn:
        result = await asyncio.wait_for(
            conn.run(command, check=False),
            timeout=timeout,
        )
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        return result.exit_status or 0, output


def _quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


# ── user provisioning ───────────────────────────────────────────────────────

# Counting-only iptables rules: no target, so packets just pass through and
# the byte counters grow. Chains are hooked into INPUT and OUTPUT at position
# 1, which does not alter any firewall decision.
_ACCOUNTING_SETUP = r"""
for IPT in iptables ip6tables; do
  command -v $IPT >/dev/null 2>&1 || continue
  $IPT -N FF-IN  2>/dev/null || true
  $IPT -N FF-OUT 2>/dev/null || true
  $IPT -C INPUT  -j FF-IN  2>/dev/null || $IPT -I INPUT  1 -j FF-IN
  $IPT -C OUTPUT -j FF-OUT 2>/dev/null || $IPT -I OUTPUT 1 -j FF-OUT
  $IPT -C FF-IN  -m owner --uid-owner $UID_$ 2>/dev/null || $IPT -A FF-IN  -m owner --uid-owner $UID_$
  $IPT -C FF-OUT -m owner --uid-owner $UID_$ 2>/dev/null || $IPT -A FF-OUT -m owner --uid-owner $UID_$
done
"""


async def provision_user(node: Node, username: str, password: str) -> None:
    """Create (or update) the VPN user on the node.

    Mirrors the desktop client's Host install: users go into the `forgefox`
    group with the restricted `ff-shell` (plain bash if the Host install
    hasn't been done), plus the iptables accounting chains.
    """
    script = (
        "set -e\n"
        "groupadd -f forgefox\n"
        "SHELL_PATH=/usr/local/bin/ff-shell\n"
        "[ -f \"$SHELL_PATH\" ] || SHELL_PATH=/bin/bash\n"
        f"if ! id -u {_quote(username)} >/dev/null 2>&1; then\n"
        f"  useradd -m -g forgefox -s \"$SHELL_PATH\" {_quote(username)}\n"
        "fi\n"
        f"echo {_quote(username + ':' + password)} | chpasswd\n"
        f"UID_=$(id -u {_quote(username)})\n"
        + _ACCOUNTING_SETUP
        + "echo PROVISION_OK\n"
    )
    code, output = await run_checked(node, script)
    if code != 0:
        raise NodeError(f"exit code {code}: {output}")
    if "PROVISION_OK" not in output:
        raise NodeError(f"неожиданный ответ: {output}")


async def block_user(node: Node, username: str) -> None:
    """Lock the account: password disabled + shell -> nologin."""
    script = (
        f"usermod -L {_quote(username)} 2>/dev/null || true\n"
        f"usermod -s /usr/sbin/nologin {_quote(username)} 2>/dev/null "
        f"|| usermod -s /sbin/nologin {_quote(username)} 2>/dev/null || true\n"
        "echo BLOCK_OK\n"
    )
    await run_checked(node, script)


async def unblock_user(node: Node, username: str) -> None:
    script = (
        f"usermod -U {_quote(username)} 2>/dev/null || true\n"
        "SHELL_PATH=/usr/local/bin/ff-shell\n"
        "[ -f \"$SHELL_PATH\" ] || SHELL_PATH=/bin/bash\n"
        f"usermod -s \"$SHELL_PATH\" {_quote(username)} 2>/dev/null || true\n"
        "echo UNBLOCK_OK\n"
    )
    await run_checked(node, script)


async def delete_user(node: Node, username: str) -> None:
    await run_node_command(
        node,
        f"id -u {_quote(username)} >/dev/null 2>&1 && userdel -r {_quote(username)} || true",
    )


async def change_password(node: Node, username: str, password: str) -> None:
    code, output = await run_checked(node, f"echo {_quote(username + ':' + password)} | chpasswd")
    if code != 0:
        raise NodeError(f"chpasswd: {output}")


# ── node setup (install.sh over SSH) ────────────────────────────────────────

SETUP_SCRIPT = f"""set -e
command -v curl >/dev/null 2>&1 || {{ export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && apt-get install -y -qq curl ca-certificates; }}
command -v gcc >/dev/null 2>&1 || {{ export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && apt-get install -y -qq gcc make; }}
command -v iptables >/dev/null 2>&1 || {{ export DEBIAN_FRONTEND=noninteractive; apt-get install -y -qq iptables; }}
curl -fsSL -o /tmp/forgefox-install.sh '{config.NODE_INSTALL_URL}'
bash /tmp/forgefox-install.sh
rm -f /tmp/forgefox-install.sh

# Restricted user environment (same as the desktop client's Host install)
groupadd -f forgefox
cat << 'FFEOF' > /usr/local/bin/ff-shell
#!/bin/bash
if [ "$1" = "-c" ]; then
    if [[ "$2" == *"forgefox-bridge"* ]] || [[ "$2" == *"python"* ]]; then
        exec sudo /bin/bash -c "$2"
    fi
fi
echo "Access restricted to ForgeFox VPN."
exit 1
FFEOF
chmod +x /usr/local/bin/ff-shell
echo "%forgefox ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/forgefox
chmod 0440 /etc/sudoers.d/forgefox
if ! grep -q "Match Group forgefox" /etc/ssh/sshd_config; then
    echo "" >> /etc/ssh/sshd_config
    echo "Match Group forgefox" >> /etc/ssh/sshd_config
    echo "    AllowTcpForwarding no" >> /etc/ssh/sshd_config
    echo "    X11Forwarding no" >> /etc/ssh/sshd_config
    echo "    PermitTunnel yes" >> /etc/ssh/sshd_config
    systemctl restart sshd 2>/dev/null || systemctl restart ssh || true
fi
echo NODE_SETUP_OK"""


async def setup_node(node: Node) -> str:
    """Run the host install on the node (idempotent). Returns the output
    tail on success, raises NodeError on failure."""
    async with await _connect(node) as conn:
        try:
            result = await asyncio.wait_for(
                conn.run(SETUP_SCRIPT, check=False),
                timeout=config.SSH_SETUP_TIMEOUT,
            )
        except asyncio.TimeoutError:
            raise NodeError(
                f"таймаут настройки ({config.SSH_SETUP_TIMEOUT // 60} мин) — "
                "нода слишком медленная или недоступна"
            )
        output = ((result.stdout or "") + (result.stderr or "")).strip()
    tail = "\n".join(output.splitlines()[-10:])
    if result.exit_status != 0:
        raise NodeError(f"exit {result.exit_status}: {tail}")
    if "NODE_SETUP_OK" not in output:
        raise NodeError(f"скрипт не дошёл до конца: {tail}")
    return output


# ── probes ──────────────────────────────────────────────────────────────────

# Line 1: CPU%, MemAvailable MB, MemTotal MB, rx bytes, tx bytes, uptime sec
#         (lo excluded).
# Line 2: ForgeFox install markers — ff-shell, forgefox group, bridge binary,
#         sudoers rule, sshd Match block. The bridge binary is what "VPN
#         stack installed" means; ff-shell + group are the panel's user
#         management layer (client-side Host installs don't create them).
# Line 3: "DISK <used%> SESSIONS <user,user,...>" — root-disk usage and the
#         forgefox-group users with an active SSH session (= live VPN users).
_MONITOR_CMD = (
    "echo \"$(top -bn1 | awk '/Cpu\\(s\\)/{print 100-$8; exit}') "
    "$(grep MemAvailable /proc/meminfo | awk '{print int($2/1024)}') "
    "$(grep MemTotal /proc/meminfo | awk '{print int($2/1024)}') "
    "$(awk 'NR>2{sub(/:/,\"\",$1); if($1!=\"lo\"){rx+=$2; tx+=$10}} END{print rx, tx}' /proc/net/dev) "
    "$(cut -d. -f1 /proc/uptime)\"; "
    "FF=0; [ -f /usr/local/bin/ff-shell ] && FF=1; "
    "GR=0; getent group forgefox >/dev/null 2>&1 && GR=1; "
    "BR=0; command -v forgefox-bridge >/dev/null 2>&1 && BR=1; "
    "[ -x /usr/local/bin/forgefox-bridge ] && BR=1; "
    "SU=0; [ -f /etc/sudoers.d/forgefox ] && SU=1; "
    "SS=0; grep -q 'Match Group forgefox' /etc/ssh/sshd_config 2>/dev/null && SS=1; "
    "echo \"$FF $GR $BR $SU $SS\"; "
    "SESS=''; for u in $(who 2>/dev/null | awk '{print $1}' | sort -u); do "
    "id -nG \"$u\" 2>/dev/null | tr ' ' '\\n' | grep -qx forgefox && SESS=\"$SESS$u,\"; done; "
    "echo \"DISK $(df -P / 2>/dev/null | awk 'NR==2{gsub(/%/,\"\",$5); print int($5)}') SESSIONS $SESS\""
)


def _parse_install_flags(lines: list[str]) -> dict:
    """Parse the marker line into the install status dict."""
    vpn = {"installed": None, "bridge": False, "ff_shell": False,
           "group": False, "sudoers": False, "sshd": False, "known": False}
    for line in lines:
        parts = line.split()
        if len(parts) == 5 and all(p in ("0", "1") for p in parts):
            ff, gr, br, su, ss = (p == "1" for p in parts)
            vpn = {
                "installed": br,          # bridge binary = VPN stack itself
                "bridge": br,
                "ff_shell": ff,
                "group": gr,
                "sudoers": su,
                "sshd": ss,
                "known": True,
            }
            break
    return vpn


def _parse_disk_sessions(lines: list[str]) -> tuple[int | None, list[str]]:
    """Parse the 'DISK <n> SESSIONS u1,u2,' line."""
    disk: int | None = None
    sessions: list[str] = []
    for line in lines:
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "DISK":
            try:
                disk = int(parts[1])
            except ValueError:
                disk = None
            if "SESSIONS" in parts:
                idx = parts.index("SESSIONS")
                sessions = [u for u in " ".join(parts[idx + 1:]).split(",") if u]
            break
    return disk, sessions


async def check_install(node: Node) -> dict:
    """Standalone install-status probe (used when a node is added)."""
    out = await run_node_command(node, _MONITOR_CMD, timeout=20)
    return _parse_install_flags(out.splitlines())

# Per forgefox-group user: "username rx_bytes tx_bytes" lines from the
# FF-IN / FF-OUT accounting chains. If a chain is missing (node rebooted
# before any re-provision), the rules are re-added so counters resume.
_TRAFFIC_CMD = r"""
for IPT in iptables; do
  command -v $IPT >/dev/null 2>&1 || exit 0
done
getent group forgefox >/dev/null 2>&1 || exit 0
iptables -N FF-IN  2>/dev/null || true
iptables -N FF-OUT 2>/dev/null || true
iptables -C INPUT  -j FF-IN  2>/dev/null || iptables -I INPUT  1 -j FF-IN
iptables -C OUTPUT -j FF-OUT 2>/dev/null || iptables -I OUTPUT 1 -j FF-OUT
for u in $(getent group forgefox | cut -d: -f4 | tr ',' ' '); do
  [ -n "$u" ] || continue
  uid=$(id -u "$u" 2>/dev/null) || continue
  iptables -C FF-IN  -m owner --uid-owner "$uid" 2>/dev/null || iptables -A FF-IN  -m owner --uid-owner "$uid"
  iptables -C FF-OUT -m owner --uid-owner "$uid" 2>/dev/null || iptables -A FF-OUT -m owner --uid-owner "$uid"
  rx=$(iptables -L FF-IN  -vnx | awk -v u="$uid" '$NF==u && $(NF-1)=="match"{print $2}')
  tx=$(iptables -L FF-OUT -vnx | awk -v u="$uid" '$NF==u && $(NF-1)=="match"{print $2}')
  echo "$u ${rx:-0} ${tx:-0}"
done
"""


async def check_alive(node: Node) -> bool:
    try:
        out = await run_node_command(node, "echo OK", timeout=15)
        return out.strip() == "OK"
    except NodeError:
        return False


async def probe_monitor(node: Node) -> dict:
    """Probe CPU/RAM/traffic/uptime + ForgeFox install markers. Raises
    NodeError when SSH fails."""
    out = await run_node_command(node, _MONITOR_CMD, timeout=20)
    lines = out.splitlines()
    parts = lines[0].split() if lines else []
    if len(parts) < 6:
        raise NodeError(f"неожиданный ответ мониторинга: {out!r}")
    cpu, mem_avail, mem_total, rx, tx, uptime = (float(p) for p in parts[:6])
    vpn = _parse_install_flags(lines[1:] if len(lines) > 1 else [])
    disk, sessions = _parse_disk_sessions(lines[1:] if len(lines) > 1 else [])

    # Rates: compare with the previous probe snapshot (kept in memory; after
    # a panel restart the first response just reports 0 kbps).
    now = time.monotonic()
    rx_kbps = tx_kbps = 0.0
    prev = _last_net.get(node.id)
    if prev:
        dt = now - prev[0]
        if dt > 1:
            rx_kbps = max(0.0, (rx - prev[1])) * 8 / dt / 1000
            tx_kbps = max(0.0, (tx - prev[2])) * 8 / dt / 1000
    _last_net[node.id] = (now, int(rx), int(tx))

    return {
        "id": node.id,
        "cpu_percent": cpu,
        "ram_mb": max(0.0, mem_total - mem_avail),  # used
        "ram_total_mb": mem_total,
        "rx_bytes": int(rx),
        "tx_bytes": int(tx),
        "rx_kbps": round(rx_kbps, 1),
        "tx_kbps": round(tx_kbps, 1),
        "uptime_seconds": int(uptime),
        "online": True,
        "vpn": vpn,
        "disk_percent": disk,
        "sessions": sessions,
    }


def offline_monitor(node_id: str) -> dict:
    return {
        "id": node_id,
        "cpu_percent": 0.0, "ram_mb": 0.0, "ram_total_mb": 0.0,
        "rx_bytes": 0, "tx_bytes": 0, "rx_kbps": 0.0, "tx_kbps": 0.0,
        "uptime_seconds": 0, "online": False,
        "vpn": {"installed": None, "known": False},
        "disk_percent": None,
        "sessions": [],
    }


async def probe_traffic(node: Node) -> dict[str, tuple[int, int]]:
    """Return {username: (rx_bytes, tx_bytes)} from the accounting chains."""
    out = await run_node_command(node, _TRAFFIC_CMD, timeout=30)
    result: dict[str, tuple[int, int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3:
            try:
                result[parts[0]] = (int(parts[1]), int(parts[2]))
            except ValueError:
                continue
    return result
