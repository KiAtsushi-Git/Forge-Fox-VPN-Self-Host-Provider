-- Python panel: new tables and columns for the features the Rust build
-- stubbed out — persistent sessions, key/value settings, traffic history,
-- per-day node stats, client status and node geo/counters.
--
-- ADD COLUMN statements rely on the migration runner's tolerance for
-- "duplicate column"/"already exists" errors, so this file is safe to run
-- on databases that already have some of the columns.

-- Sessions persisted in the DB (survive panel restarts).
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

-- Key/value settings (Telegram bot, poll intervals, notifications).
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Per-client per-day traffic bytes (feeds the dashboard chart + limits).
CREATE TABLE IF NOT EXISTS traffic_history (
    day TEXT NOT NULL,
    client_id TEXT NOT NULL,
    bytes BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (day, client_id)
);

-- Per-node per-day network counters (rx/tx deltas from the node poller).
CREATE TABLE IF NOT EXISTS node_stats (
    day TEXT NOT NULL,
    node_id TEXT NOT NULL,
    rx_bytes BIGINT NOT NULL DEFAULT 0,
    tx_bytes BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (day, node_id)
);

-- Client lifecycle: active / blocked / expired / limited (auto-set by the
-- traffic collector, or manually from the UI).
ALTER TABLE clients ADD COLUMN status TEXT DEFAULT 'active';
UPDATE clients SET status = 'active' WHERE status IS NULL OR status = '';

-- Who performed the action (audit entries now carry the admin name).
-- "user" is quoted: it is a reserved word in Postgres (plain SQL here,
-- the ORM quotes it automatically everywhere else).
ALTER TABLE audit_log ADD COLUMN "user" TEXT;
UPDATE audit_log SET "user" = 'system' WHERE "user" IS NULL OR "user" = '';

-- Node extras: best-effort geo tag + last /proc/net/dev counters.
ALTER TABLE nodes ADD COLUMN country TEXT;
ALTER TABLE nodes ADD COLUMN last_rx BIGINT;
ALTER TABLE nodes ADD COLUMN last_tx BIGINT;
