-- Node install status: is the ForgeFox VPN stack (forgefox-bridge) present
-- on the node? 'unknown' / 'yes' / 'no'. Checked when a node is added, on
-- /api/nodes/:id/check and by the background poller.
ALTER TABLE nodes ADD COLUMN installed TEXT;
UPDATE nodes SET installed = 'unknown' WHERE installed IS NULL;

-- Live VPN sessions (forgefox-group users with an active SSH session),
-- comma-separated usernames, refreshed by the background poller.
ALTER TABLE nodes ADD COLUMN sessions TEXT;

