"""Database engine + migration runner.

The panel supports both SQLite and PostgreSQL off one codebase (the same
pairing the Rust/sqlx build had). The migration runner keeps existing
databases working:

  * fresh databases get every migration applied in filename order;
  * databases created by the old Rust panel carry a `_sqlx_migrations`
    table — those versions are recognised and skipped;
  * applied migrations are tracked in `_panel_migrations`.

Statements are split on ';' and executed one by one; "duplicate column" /
"already exists" errors are ignored so the ADD COLUMN migrations re-run
harmlessly on databases that already have the columns.
"""

import logging
import re
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from . import config

log = logging.getLogger("forgefox.db")

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker | None = None
DIALECT = "sqlite"


def engine() -> AsyncEngine:
    global _engine, DIALECT
    if _engine is None:
        url, DIALECT = config.sqlalchemy_url()
        kwargs: dict = {"pool_pre_ping": True}
        if DIALECT == "postgres":
            kwargs["pool_size"] = 5
        _engine = create_async_engine(url, **kwargs)
    return _engine


def sessions() -> async_sessionmaker:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(engine(), expire_on_commit=False)
    return _session_factory


def is_sqlite() -> bool:
    return DIALECT == "sqlite"


# ── migration runner ────────────────────────────────────────────────────────

_TABLE_RE = re.compile(r"create\s+table\s+(if\s+not\s+exists\s+)?[\"'`]?(?P<name>\w+)", re.I)
_BENIGN = ("duplicate column", "already exists")


async def _applied_by_sqlx(conn) -> set[str]:
    """Filenames the old Rust panel applied (from _sqlx_migrations)."""
    names: set[str] = set()
    # Check the table exists WITHOUT raising: on Postgres a failed SELECT
    # aborts the surrounding transaction, and every later statement in it
    # would fail with InFailedSQLTransactionError.
    if is_sqlite():
        rows = await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='_sqlx_migrations'")
        )
        if not rows.fetchall():
            return names
    else:
        rows = await conn.execute(text("SELECT to_regclass('_sqlx_migrations')"))
        if rows.scalar() is None:
            return names
    rows = await conn.execute(text("SELECT version, description FROM _sqlx_migrations"))
    for version, description in rows.fetchall():
        # sqlx stores version 20260908000000 / description "init" for
        # the file 20260908000000_init.sql
        if version is not None and description:
            names.add(f"{int(version)}_{description}.sql")
    return names


def _strip_comments(stmt: str) -> str:
    """Drop `-- ...` lines: text() treats `:name` inside comments (e.g. the
    `/sub/:id` mention in a migration header) as bind parameters."""
    return "\n".join(
        line for line in stmt.splitlines() if not line.strip().startswith("--")
    ).strip()


async def _execute_statement(conn, stmt: str) -> None:
    stmt = _strip_comments(stmt)
    if not stmt:
        return
    # On Postgres a failed statement poisons the whole transaction, so a
    # benign "already exists" must be rolled back to a savepoint or every
    # later statement of the migration would fail. SQLite has no such
    # concept — errors leave the transaction usable.
    if not is_sqlite():
        await conn.execute(text("SAVEPOINT ff_stmt"))
    try:
        await conn.execute(text(stmt))
    except Exception as e:
        msg = str(e).lower()
        if any(b in msg for b in _BENIGN):
            if not is_sqlite():
                await conn.execute(text("ROLLBACK TO SAVEPOINT ff_stmt"))
            log.debug("Skipping already-applied statement: %.80s", stmt.splitlines()[0])
            return
        raise
    if not is_sqlite():
        await conn.execute(text("RELEASE SAVEPOINT ff_stmt"))


async def run_migrations() -> None:
    eng = engine()
    async with eng.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS _panel_migrations ("
                "filename TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
        )
        sqlx_applied = await _applied_by_sqlx(conn)
        rows = await conn.execute(text("SELECT filename FROM _panel_migrations"))
        panel_applied = {r[0] for r in rows.fetchall()}

        files = sorted(p.name for p in config.MIGRATIONS_DIR.glob("*.sql"))
        for filename in files:
            if filename in panel_applied or filename in sqlx_applied:
                continue
            log.info("Applying migration %s", filename)
            sql = (config.MIGRATIONS_DIR / filename).read_text(encoding="utf-8")
            # Strip `-- ...` comment lines BEFORE splitting: comments may
            # contain semicolons (they do), which would split mid-comment.
            sql = "\n".join(
                line for line in sql.splitlines() if not line.strip().startswith("--")
            )
            # Split on ';' — the migrations contain no triggers or
            # semicolons inside string literals, so a plain split is safe.
            for stmt in sql.split(";"):
                stmt = stmt.strip()
                if not stmt:
                    continue
                await _execute_statement(conn, stmt)
            await conn.execute(
                text("INSERT INTO _panel_migrations (filename, applied_at) VALUES (:f, :t)"),
                {"f": filename, "t": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
            )
