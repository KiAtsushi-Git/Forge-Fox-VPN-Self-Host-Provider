#!/bin/bash
# ForgeFox VPN Provider - Installation Script
# Usage:
#   curl -Ls https://raw.githubusercontent.com/KiAtsushi-Git/Forge-Fox-VPN-Self-Host-Provider/main/install.sh | bash -s -- --db sqlite --user admin --pass secret

set -e

DB_TYPE="sqlite"
ADMIN_USER="admin"
ADMIN_PASS="admin"
IMAGE="ghcr.io/kiatsushi-git/forgefoxvpn-provider:latest"
REPO_URL="https://github.com/KiAtsushi-Git/Forge-Fox-VPN-Self-Host-Provider.git"
PORT="8080"

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

echo "=========================================="
echo "🦊 Installing ForgeFox VPN Provider Panel"
echo "=========================================="
echo "DB Type: $DB_TYPE"
echo "Admin User: $ADMIN_USER"

# Check root
if [ "$EUID" -ne 0 ]; then
  echo "Please run as root (or with sudo)"
  exit 1
fi

# Install Docker if not exists
if ! command -v docker &> /dev/null; then
    echo "Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
fi

# Compose command (new plugin or legacy standalone)
compose_up() {
    if docker compose version &> /dev/null; then
        docker compose up -d
    elif command -v docker-compose &> /dev/null; then
        docker-compose up -d
    else
        echo "❌ docker compose not found. Install it manually: https://docs.docker.com/compose/install/"
        exit 1
    fi
}

INSTALL_DIR="/opt/forgefox-provider"
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

# Get the image: pull from ghcr, or build locally from source as fallback
if ! docker pull "$IMAGE" 2>/dev/null; then
    echo "⚠️  Image $IMAGE not available on ghcr.io."
    echo "   Building from source (this may take 5-15 minutes on a small VPS)..."
    if [ ! -d "$INSTALL_DIR/src" ]; then
        rm -rf "$INSTALL_DIR/build"
        git clone --depth 1 "$REPO_URL" "$INSTALL_DIR/build"
    fi
    docker build -t forgefox-provider:local "$INSTALL_DIR/build"
    IMAGE="forgefox-provider:local"
fi

# Values are passed to compose via .env (no fragile sed on special characters)
cat > .env <<EOF
IMAGE=$IMAGE
ADMIN_USER=$ADMIN_USER
ADMIN_PASS=$ADMIN_PASS
EOF

if [ "$DB_TYPE" = "postgres" ]; then
    cat << 'EOF' > docker-compose.yml
services:
  forgefox-panel:
    image: ${IMAGE}
    container_name: forgefox_panel
    restart: always
    network_mode: host
    environment:
      - DATABASE_URL=postgres://forgefox:forgefox@127.0.0.1:5432/forgefox
      - ADMIN_USER=${ADMIN_USER}
      - ADMIN_PASS=${ADMIN_PASS}
    depends_on:
      - db
  db:
    image: postgres:15-alpine
    container_name: forgefox_db
    restart: always
    environment:
      POSTGRES_USER: forgefox
      POSTGRES_PASSWORD: forgefox
      POSTGRES_DB: forgefox
    ports:
      - "127.0.0.1:5432:5432"
    volumes:
      - forgefox_db_data:/var/lib/postgresql/data
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

# Open the firewall port if a firewall is active
if command -v ufw &> /dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
    echo "Opening port $PORT/tcp in ufw..."
    ufw allow "$PORT/tcp"
elif command -v firewall-cmd &> /dev/null && firewall-cmd --state &> /dev/null; then
    echo "Opening port $PORT/tcp in firewalld..."
    firewall-cmd --permanent --add-port="$PORT/tcp"
    firewall-cmd --reload
fi

echo "Starting Provider Panel..."
compose_up

# Wait for the panel to come up
echo "Waiting for the panel to start..."
PANEL_OK=0
for i in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:$PORT" -o /dev/null 2>/dev/null; then
        PANEL_OK=1
        break
    fi
    sleep 2
done

# Figure out the server IP (with timeout and fallback)
SERVER_IP=$(curl -s --max-time 5 ifconfig.me 2>/dev/null || true)
if [ -z "$SERVER_IP" ]; then
    SERVER_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
fi

echo "=========================================="
if [ "$PANEL_OK" -eq 1 ]; then
    echo "✅ Installation Complete!"
    echo "The Provider panel is available at http://$SERVER_IP:$PORT"
else
    echo "⚠️  Container is running, but the panel did not respond on port $PORT in time."
    echo "   Check logs: docker logs forgefox_panel"
    echo "   If it responds locally but not from your PC — open port $PORT in your cloud provider's security group / iptables."
fi
echo "Login: $ADMIN_USER"
echo "Password: $ADMIN_PASS"
echo "=========================================="
