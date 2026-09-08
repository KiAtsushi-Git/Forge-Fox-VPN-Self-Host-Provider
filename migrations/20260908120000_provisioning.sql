-- Node SSH credentials (used to provision VPN users on the node)
ALTER TABLE nodes ADD COLUMN ssh_pass TEXT;

-- SSH password of the VPN user created on the node (returned via /sub/:id)
ALTER TABLE clients ADD COLUMN password TEXT;
