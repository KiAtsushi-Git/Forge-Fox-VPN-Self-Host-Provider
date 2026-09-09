#!/bin/bash
# ForgeFox VPN Provider - Installation Script
# Installs everything from scratch on a clean Ubuntu/Debian system:
# curl, git, docker (if missing), then builds and runs the panel.
#
# Usage:
#   bash provider_install.sh --db sqlite --user admin --pass secret
#   bash provider_install.sh --db postgres --user admin --pass secret --port 8080

# Exit on error, undefined var, and pipe failure so a broken step can't
# silently report success (the old `curl | bash` failure mode).
set -euo pipefail

DB_TYPE="sqlite"
ADMIN_USER="admin"
ADMIN_PASS="admin"
IMAGE="forgefox-provider:local"
REPO_URL="https://github.com/KiAtsushi-Git/Forge-Fox-VPN-Self-Host-Provider.git"
PORT="8080"
INSTALL_DIR="/opt/forgefox-provider"

log() { echo "[forgefox] $*"; }
die() { echo "[forgefox] ERROR: $*" >&2; exit 1; }

# Parse arguments
while [[ "$#" -gt 0 ]]; do
    case $1 in
        --db) DB_TYPE="$2"; shift ;;
        --user) ADMIN_USER="$2"; shift ;;
        --pass) ADMIN_PASS="$2"; shift ;;
        --port) PORT="$2"; shift ;;
        *) echo "Unknown parameter passed: $1"; exit 1 ;;
    esac
    shift
done

[ "$DB_TYPE" = "sqlite" ] || [ "$DB_TYPE" = "postgres" ] || die "--db must be sqlite or postgres"
[ -n "$ADMIN_USER" ] || die "--user must not be empty"
[ -n "$ADMIN_PASS" ] || die "--pass must not be empty"

# WSL has no systemd PID 1; detect it once so docker steps can adapt.
IS_WSL=0
if grep -qi microsoft /proc/version 2>/dev/null; then
    IS_WSL=1
    log "WSL environment detected"
fi

# ---- Root check -----------------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
    die "Please run as root (or with sudo)"
fi

log "Installing Provider Panel"
log "  DB: $DB_TYPE  |  Port: $PORT  |  Admin: $ADMIN_USER"

# ---- Base packages: a truly clean system may lack even curl/git ----------
if ! command -v curl &>/dev/null || ! command -v git &>/dev/null; then
    log "Installing base packages (curl, git, ca-certificates)..."
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq || (apt-get update && die "apt-get update failed")
    apt-get install -y -qq curl git ca-certificates rsync || die "failed to install base packages"
fi

# ---- Docker: install from scratch if missing ------------------------------
start_docker() {
    if [ "$IS_WSL" -eq 1 ]; then
        # No systemd in WSL: start dockerd directly if it isn't up.
        if ! docker info &>/dev/null; then
            log "Starting dockerd (WSL, no systemd)..."
            nohup dockerd > /tmp/dockerd.log 2>&1 &
            for i in $(seq 1 30); do
                docker info &>/dev/null && break
                sleep 2
            done
        fi
    else
        systemctl enable --now docker
    fi
    docker info &>/dev/null || die "Docker daemon is not running. Check: journalctl -u docker (or /tmp/dockerd.log on WSL)"
    log "Docker daemon is up."
}

if ! command -v docker &>/dev/null; then
    log "Installing Docker Engine from scratch..."
    curl -fsSL https://get.docker.com -o /tmp/get-docker.sh || die "failed to download get.docker.com script"
    bash /tmp/get-docker.sh || die "Docker installation failed"
    rm -f /tmp/get-docker.sh
fi
start_docker

# Compose command (new plugin or legacy standalone)
compose() {
    if docker compose version &>/dev/null; then
        docker compose "$@"
    elif command -v docker-compose &>/dev/null; then
        docker-compose "$@"
    else
        die "docker compose not found. Install it manually: https://docs.docker.com/compose/install/"
    fi
}

# ---- Get the panel image --------------------------------------------------
# The image is not published to a registry — always build from source.
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

log "Building the panel from source ($REPO_URL)..."
rm -rf "$INSTALL_DIR/build"
git clone --depth 1 "$REPO_URL" "$INSTALL_DIR/build" || die "git clone failed"
docker build -t "$IMAGE" "$INSTALL_DIR/build" || die "docker build failed"

# ---- Compose config -------------------------------------------------------
# Values are passed via .env (no fragile sed on special characters).
# ADMIN_PASS is written with chmod 600 to keep it out of casual sight.
cat > .env <<EOF
IMAGE=$IMAGE
ADMIN_USER=$ADMIN_USER
ADMIN_PASS=$ADMIN_PASS
EOF
chmod 600 .env

if [ "$DB_TYPE" = "postgres" ]; then
    # The panel runs in the host network and reaches postgres at
    # 127.0.0.1:5432. If something else on the host already owns 5432
    # (WSL images often ship a system postgres), put the container on a
    # free loopback port instead and point DATABASE_URL at it.
    PG_HOST_PORT=5432
    if ss -tlnH 2>/dev/null | grep -qE '127\.0\.0\.1:5432\b|\[::1\]:5432\b|\*:5432\b'; then
        PG_HOST_PORT=15432
        log "Port 5432 is already taken on this host — using 127.0.0.1:$PG_HOST_PORT for the panel DB."
    fi
    cat << EOF > docker-compose.yml
services:
  db:
    image: postgres:15-alpine
    container_name: forgefox_db
    restart: always
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U forgefox -d forgefox"]
      interval: 3s
      timeout: 3s
      retries: 20
    environment:
      POSTGRES_USER: forgefox
      POSTGRES_PASSWORD: forgefox
      POSTGRES_DB: forgefox
    volumes:
      - forgefox_db_data:/var/lib/postgresql/data
    ports:
      - "127.0.0.1:$PG_HOST_PORT:5432"

  forgefox-panel:
    image: ${IMAGE}
    container_name: forgefox_panel
    restart: always
    network_mode: host
    environment:
      - DATABASE_URL=postgres://forgefox:forgefox@127.0.0.1:$PG_HOST_PORT/forgefox
      - ADMIN_USER=${ADMIN_USER}
      - ADMIN_PASS=${ADMIN_PASS}
    depends_on:
      db:
        condition: service_healthy
volumes:
  forgefox_db_data:
EOF
else
    # SQLite
    cat << 'EOF' > docker-compose.yml
services:
  forgefox-panel:
    image: ${IMAGE}
    container_name: forgefox_panel
    restart: always
    network_mode: host
    environment:
      - DATABASE_URL=sqlite://data/forgefox.db?mode=rwc
      - ADMIN_USER=${ADMIN_USER}
      - ADMIN_PASS=${ADMIN_PASS}
    volumes:
      - ./data:/app/data
EOF
fi

# ---- Firewall -------------------------------------------------------------
if [ "$IS_WSL" -eq 0 ]; then
    if command -v ufw &>/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
        log "Opening port $PORT/tcp in ufw..."
        ufw allow "$PORT/tcp"
    elif command -v firewall-cmd &>/dev/null && firewall-cmd --state &>/dev/null; then
        log "Opening port $PORT/tcp in firewalld..."
        firewall-cmd --permanent --add-port="$PORT/tcp"
        firewall-cmd --reload
    fi
fi

# ---- Start ----------------------------------------------------------------
log "Starting Provider Panel..."
compose up -d

# ---- Wait for the panel ---------------------------------------------------
log "Waiting for the panel to start..."
PANEL_OK=0
for i in $(seq 1 45); do
    if curl -fsS -m 3 "http://127.0.0.1:$PORT" -o /dev/null 2>/dev/null; then
        PANEL_OK=1
        break
    fi
    # If the container died, show why and stop waiting.
    if ! docker ps --format '{{.Names}}' | grep -q '^forgefox_panel$'; then
        log "Container exited early. Last logs:"
        docker logs forgefox_panel --tail 30 2>&1 || true
        die "forgefox_panel container is not running"
    fi
    sleep 2
done

SERVER_IP=$(curl -s --max-time 5 ifconfig.me 2>/dev/null || true)
[ -n "$SERVER_IP" ] || SERVER_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
[ -n "$SERVER_IP" ] || SERVER_IP="127.0.0.1"

echo "=========================================="
if [ "$PANEL_OK" -eq 1 ]; then
    echo "✅ Installation Complete!"
    echo "The Provider panel is available at http://$SERVER_IP:$PORT"
    echo "PROVIDER_PANEL_URL=http://$SERVER_IP:$PORT"
else
    echo "⚠️  Container is running, but the panel did not respond on port $PORT in time."
    echo "   Check logs: docker logs forgefox_panel"
    echo "   If it responds locally but not from your PC — open port $PORT in your cloud provider's security group / iptables."
    exit 1
fi
echo "Login: $ADMIN_USER"
echo "Password: $ADMIN_PASS"
echo "=========================================="
