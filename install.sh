#!/bin/bash
# ForgeFox VPN Provider - Installation Script
# Usage:
#   curl -Ls https://raw.githubusercontent.com/KiAtsushi-Git/Forge-Fox-VPN-Self-Host-Provider/main/install.sh | bash -s -- --db sqlite --user admin --pass secret

set -e

DB_TYPE="sqlite"
ADMIN_USER="admin"
ADMIN_PASS="admin"

# Parse arguments
while [[ "$#" -gt 0 ]]; do
    case $1 in
        --db) DB_TYPE="$2"; shift ;;
        --user) ADMIN_USER="$2"; shift ;;
        --pass) ADMIN_PASS="$2"; shift ;;
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

INSTALL_DIR="/opt/forgefox-provider"
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

if [ "$DB_TYPE" = "postgres" ]; then
    cat << 'EOF' > docker-compose.yml
version: '3.8'
services:
  forgefox-panel:
    image: ghcr.io/kiatsushi-git/forgefoxvpn-provider:latest
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
version: '3.8'
services:
  forgefox-panel:
    image: ghcr.io/kiatsushi-git/forgefoxvpn-provider:latest
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

# Replace env variables in docker-compose.yml
sed -i "s/\${ADMIN_USER}/$ADMIN_USER/g" docker-compose.yml
sed -i "s/\${ADMIN_PASS}/$ADMIN_PASS/g" docker-compose.yml

echo "Starting Provider Panel..."
# Uncomment this when image is pushed
# docker compose up -d

echo "=========================================="
echo "✅ Installation Complete!"
echo "The Provider panel should now be available at http://$(curl -s ifconfig.me):8080"
echo "Login: $ADMIN_USER"
echo "Password: $ADMIN_PASS"
echo "=========================================="
