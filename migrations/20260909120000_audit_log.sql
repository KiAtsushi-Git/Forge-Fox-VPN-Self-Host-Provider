-- Real audit log (replaces the mock logs endpoint).
-- No user column: the panel has a single admin account; the API returns (time, action).
-- id TEXT + uuid keeps the schema identical on SQLite and PostgreSQL.
CREATE TABLE IF NOT EXISTS audit_log (
    id TEXT PRIMARY KEY,
    time TEXT NOT NULL,
    action TEXT NOT NULL
);
