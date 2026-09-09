-- Repair rows written by the old INSERT, which the sqlx Any driver typed
-- as TEXT on SQLite: expiry became 0 (CAST '' AS TIMESTAMP), limit_gb ''
-- (empty string). CAST them back to their declared INTEGER types; the
-- empty-string limit means "unlimited", which is NULL.
--
-- CAST('' AS INTEGER) is 0 on SQLite, hence the NULLIF guard. Runs as
-- SELECT-style UPDATE on PostgreSQL too (harmless: typed columns there).
UPDATE clients SET
    limit_gb = CASE WHEN limit_gb IS NULL OR CAST(limit_gb AS TEXT) = ''
                    THEN NULL ELSE CAST(limit_gb AS INTEGER) END,
    used_bytes = COALESCE(CAST(used_bytes AS INTEGER), 0),
    expiry = CASE WHEN expiry IS NOT NULL AND CAST(expiry AS TEXT) IN ('0', '')
                  THEN NULL ELSE expiry END;
