"""ForgeFox VPN Provider — application entry point.

Run with `python -m app.main` (or uvicorn app.main:app). On startup the app
applies migrations, seeds the admin account from the environment and starts
the background jobs (node polling, traffic collection, Telegram bot).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from . import audit as audit_mod, config, db, jobs, security
from .models import Admin
from .routers import auth, clients, monitoring, nodes, settings, stats, subscription, update

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("forgefox")


async def seed_admin() -> None:
    """Sync admin credentials from the environment (install.sh --user/--pass)
    into the admins table so install-time credentials always work."""
    async with db.sessions()() as session:
        rows = await session.execute(select(Admin).where(Admin.username == config.ADMIN_USER))
        admin = rows.scalars().first()
        if admin is None:
            session.add(
                Admin(
                    id=security.new_id(),
                    username=config.ADMIN_USER,
                    password_hash=security.hash_password(config.ADMIN_PASS),
                    created_at=None,
                )
            )
        else:
            admin.password_hash = security.hash_password(config.ADMIN_PASS)
        await session.commit()
    log.info("Admin user '%s' is ready", config.ADMIN_USER)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.run_migrations()
    await seed_admin()
    tasks = await jobs.start_all()
    log.info("ForgeFox VPN Provider started (version %s)", config.UPDATE_COMMIT)
    try:
        yield
    finally:
        jobs.request_stop()
        for t in tasks:
            t.cancel()


app = FastAPI(title="ForgeFox VPN Provider", lifespan=lifespan)

# Public routes
app.include_router(auth.router)
app.include_router(subscription.router)
# Authenticated API
app.include_router(nodes.router)
app.include_router(clients.router)
app.include_router(monitoring.router)
app.include_router(stats.router)
app.include_router(settings.router)
app.include_router(update.router)

# Frontend (mounted last so /api and /sub win). html=True serves index.html
# for unknown paths — the SPA has no client-side routes, so this is just a
# friendly fallback.
if config.PUBLIC_DIR.exists():
    app.mount("/", StaticFiles(directory=config.PUBLIC_DIR, html=True), name="static")
else:
    log.warning("public/ not found at %s — serving API only", config.PUBLIC_DIR)


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="info")


if __name__ == "__main__":
    main()
