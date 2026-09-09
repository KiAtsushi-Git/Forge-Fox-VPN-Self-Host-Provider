#!/bin/bash
# ForgeFox VPN Provider — self-update script.
# Called by the panel itself (POST /api/update/run) with the host's docker
# socket and this script bind-mounted into the container (see install.sh).
# Safe to run by hand too:  bash /opt/forgefox-provider/update.sh

set -e
INSTALL_DIR="/opt/forgefox-provider"
REPO_URL="https://github.com/KiAtsushi-Git/Forge-Fox-VPN-Self-Host-Provider.git"
IMAGE="forgefox-provider:local"
cd "$INSTALL_DIR"

echo "[$(date '+%F %T')] ForgeFox Provider self-update started"

# Refresh the source (full clone, not depth 1: tags are needed for versioning)
if [ -d "$INSTALL_DIR/src" ] || [ -d "$INSTALL_DIR/build" ]; then
    rm -rf "$INSTALL_DIR/build"
fi
git clone "$REPO_URL" "$INSTALL_DIR/build" 2>/dev/null || git clone --depth 1 "$REPO_URL" "$INSTALL_DIR/build"

# Stamp the commit into the image so /api/update can compare this build
# against the tip of main on the next check.
UPDATE_COMMIT=$(git -C "$INSTALL_DIR/build" rev-parse HEAD)
echo "[$(date '+%F %T')] Building commit $UPDATE_COMMIT"

# Rebuild the image from the fresh source
docker build --progress=plain --build-arg UPDATE_COMMIT="$UPDATE_COMMIT" -t "$IMAGE" "$INSTALL_DIR/build"

# Update the scripts (install.sh, update.sh) from the repo
cp "$INSTALL_DIR/build/install.sh" "$INSTALL_DIR/install.sh" 2>/dev/null || true
cp "$INSTALL_DIR/build/update.sh"  "$INSTALL_DIR/update.sh"  2>/dev/null || true
chmod +x "$INSTALL_DIR/install.sh" "$INSTALL_DIR/update.sh"

# Recreate the panel container with the new image (keeps .env / compose / data)
# The DB container (forgefox_db, if any) is untouched.
IMAGE="$IMAGE" docker compose up -d --no-deps --force-recreate forgefox-panel 2>/dev/null \
    || IMAGE="$IMAGE" docker-compose up -d --no-deps --force-recreate forgefox-panel

echo "[$(date '+%F %T')] Self-update finished"
