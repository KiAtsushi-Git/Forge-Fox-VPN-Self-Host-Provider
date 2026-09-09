-- Multi-node clients: a client can now live on several nodes at once.
-- node_ids is a comma-separated node id list (kept as TEXT so the schema
-- stays identical on SQLite and PostgreSQL); node_id is kept for
-- backwards compatibility with old rows and old client builds.
ALTER TABLE clients ADD COLUMN node_ids TEXT;

-- Backfill: every legacy single-node client gets a one-element list.
UPDATE clients SET node_ids = node_id WHERE node_ids IS NULL OR node_ids = '';
