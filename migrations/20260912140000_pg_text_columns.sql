-- DIALECT: postgres
-- The Rust panel created these columns as TIMESTAMP; the Python panel
-- stores datetime as TEXT strings everywhere, and asyncpg sends strictly
-- typed parameters, so VARCHAR values are rejected by timestamp columns
-- (SQLite is dynamically typed and never cared). Convert the columns so
-- the ORM and the schema agree. The Rust code always supplied the values
-- explicitly, so the CURRENT_TIMESTAMP defaults never fired — drop them
-- instead of leaving a timestamp default on a text column.
ALTER TABLE admins  ALTER COLUMN created_at TYPE TEXT USING created_at::text;
ALTER TABLE admins  ALTER COLUMN created_at DROP DEFAULT;
ALTER TABLE nodes   ALTER COLUMN created_at TYPE TEXT USING created_at::text;
ALTER TABLE nodes   ALTER COLUMN created_at DROP DEFAULT;
ALTER TABLE clients ALTER COLUMN created_at TYPE TEXT USING created_at::text;
ALTER TABLE clients ALTER COLUMN created_at DROP DEFAULT;
ALTER TABLE clients ALTER COLUMN expiry     TYPE TEXT USING expiry::text;
