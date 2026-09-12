"""Update check (GitHub) + self-update (runs the host's update.sh through
the mounted docker socket)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException

from .. import audit as audit_mod, config
from .deps import require_auth

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])
log = logging.getLogger("forgefox.update")


def _unavailable(reason: str) -> dict:
    return {
        "current_version": config.UPDATE_COMMIT,
        "latest_version": "",
        "update_available": False,
        "release_url": "",
        "release_notes": reason,
    }


@router.get("/update")
async def get_update_info():
    """Compare the running build's commit against the tip of main on GitHub.
    The build is stamped with UPDATE_COMMIT at docker-build time."""
    current = config.UPDATE_COMMIT
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"https://api.github.com/repos/{config.UPDATE_REPO}/commits/main",
                headers={"User-Agent": "forgefox-provider", "Accept": "application/vnd.github+json"},
            )
        if resp.status_code != 200:
            return _unavailable(f"GitHub ответил HTTP {resp.status_code}")
        data = resp.json()
    except Exception as e:
        return _unavailable(f"нет соединения с GitHub: {e}")

    latest = data.get("sha") or ""
    short = latest[:7] if len(latest) >= 7 else latest
    message = (data.get("commit", {}).get("message") or "").splitlines()
    update_available = bool(latest) and (current == "unknown" or not latest.startswith(current))
    return {
        "current_version": current,
        "latest_version": short,
        "update_available": update_available,
        "release_url": data.get("html_url") or "",
        "release_notes": message[0] if message else "",
    }


@router.post("/update/run")
async def run_self_update(user: str = Depends(require_auth)):
    """Spawn /app/update.sh detached — it rebuilds the image and recreates
    THIS container, severing the connection. The UI polls /api/update until
    the new version answers."""
    script = Path("/app/update.sh")
    if not script.exists():
        raise HTTPException(
            status_code=409,
            detail=(
                "Скрипт обновления не найден. Панель установлена старой версией "
                "install.sh — обновите вручную: curl -Ls https://raw.githubusercontent.com/"
                f"{config.UPDATE_REPO}/main/install.sh | bash"
            ),
        )
    await audit_mod.audit(user, "Запущено самообновление панели")
    # Detached: this process (and its HTTP response) may be killed mid-update.
    try:
        os.system(
            f"nohup bash {script} > /opt/forgefox-provider/update.log 2>&1 &"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"не удалось запустить обновление: {e}")
    return {"status": "updating"}
