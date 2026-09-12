"""Runtime configuration: everything comes from the environment so the
docker-compose file written by install.sh stays the single source of truth."""

import os
from pathlib import Path

# Repo root = parent of the app/ package. migrations/ and public/ live there.
BASE_DIR = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = BASE_DIR / "migrations"
PUBLIC_DIR = BASE_DIR / "public"

# DATABASE_URL decides the backend (same convention as the Rust panel):
#   postgres://...  -> PostgreSQL
#   sqlite://...    -> SQLite (anything else / empty -> local forgefox.db)
# The URL may carry the sqlx-style "?mode=rwc" suffix; it is stripped here.
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "admin")

PORT = int(os.environ.get("PORT", "8080"))
HOST = os.environ.get("HOST", "0.0.0.0")

# Commit the image was built from; stamped by install.sh / update.sh via
# docker build --build-arg. /api/update compares it to the tip of main.
UPDATE_COMMIT = os.environ.get("UPDATE_COMMIT", "unknown")

SESSION_TTL_SECONDS = 24 * 60 * 60

# SSH budgets: quick calls (provision/monitoring) vs. node setup, which runs
# install.sh and compiles the TUN bridge — minutes on slow VPSes.
SSH_PROVISION_TIMEOUT = 30
SSH_SETUP_TIMEOUT = 600

UPDATE_REPO = "KiAtsushi-Git/Forge-Fox-VPN-Self-Host-Provider"
NODE_INSTALL_URL = (
    "https://raw.githubusercontent.com/KiAtsushi-Git/Forge-Fox-VPN/main/windows/install.sh"
)


def sqlalchemy_url() -> tuple[str, str]:
    """Normalize DATABASE_URL into a SQLAlchemy async URL.

    Returns (url, dialect) where dialect is "sqlite" or "postgres".
    """
    raw = DATABASE_URL
    if not raw or raw.startswith("sqlite"):
        # "sqlite://data/forgefox.db?mode=rwc" -> relative file data/forgefox.db
        path = raw[len("sqlite://"):] if raw.startswith("sqlite://") else ""
        path = path.split("?", 1)[0].lstrip("/")
        if not path:
            path = "forgefox.db"
        # The SQLite file may sit in a mounted volume dir that does not
        # exist yet on a fresh install.
        parent = Path(path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite+aiosqlite:///{path}", "sqlite"
    # postgres:// or postgresql://
    raw = raw.split("?", 1)[0]
    if raw.startswith("postgresql+asyncpg://"):
        return raw, "postgres"
    for prefix in ("postgres://", "postgresql://"):
        if raw.startswith(prefix):
            return "postgresql+asyncpg://" + raw[len(prefix):], "postgres"
    raise ValueError(f"Unsupported DATABASE_URL: {raw!r}")
