#!/usr/bin/env bash
set -Eeuo pipefail

# =============================================================================
# VPN-3X — Ubuntu installer
#
# Supported:
#   Ubuntu 22.04 LTS
#   Ubuntu 24.04 LTS
#
# Run:
#   sudo bash install.sh
#
# The script must be executed from the repository root.
# =============================================================================

APP_NAME="vpn-3x"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${APP_DIR}/.env"
MIGRATION_VENV="${APP_DIR}/.venv-migrate"

COMPOSE="docker compose"

log() {
    echo
    echo "============================================================"
    echo "$1"
    echo "============================================================"
}

info() {
    echo "[INFO] $1"
}

warn() {
    echo "[WARN] $1"
}

die() {
    echo
    echo "[ERROR] $1"
    exit 1
}

cleanup() {
    local exit_code=$?
    if [[ $exit_code -ne 0 ]]; then
        echo
        echo "============================================================"
        echo "Installation failed."
        echo "Exit code: ${exit_code}"
        echo "============================================================"
        echo
        echo "Useful diagnostics:"
        echo "  cd ${APP_DIR}"
        echo "  docker compose ps"
        echo "  docker compose logs --tail=100"
        echo
    fi
}

trap cleanup EXIT

# =============================================================================
# Basic checks
# =============================================================================

log "Checking system"

if [[ "${EUID}" -ne 0 ]]; then
    die "Run this installer as root: sudo bash install.sh"
fi

if [[ ! -f "${APP_DIR}/docker-compose.yml" && ! -f "${APP_DIR}/compose.yml" ]]; then
    die "docker-compose.yml / compose.yml not found. Run install.sh from the VPN-3X repository root."
fi

if [[ ! -f "${APP_DIR}/server/requirements.txt" ]]; then
    die "server/requirements.txt not found."
fi

if [[ ! -f "${APP_DIR}/migrations/alembic.ini" ]]; then
    die "migrations/alembic.ini not found."
fi

if [[ ! -f "${APP_DIR}/migrations/env.py" ]]; then
    die "migrations/env.py not found."
fi

if [[ ! -f /etc/os-release ]]; then
    die "/etc/os-release not found."
fi

source /etc/os-release

if [[ "${ID:-}" != "ubuntu" ]]; then
    die "This installer supports Ubuntu only. Detected: ${PRETTY_NAME:-unknown}"
fi

case "${VERSION_ID:-}" in
    22.04|24.04)
        ;;
    *)
        warn "Detected Ubuntu ${VERSION_ID:-unknown}."
        warn "This script is tested primarily with Ubuntu 22.04 and 24.04."
        read -r -p "Continue anyway? [y/N]: " answer
        if [[ ! "${answer}" =~ ^[Yy]$ ]]; then
            exit 1
        fi
        ;;
esac

cd "${APP_DIR}"

# =============================================================================
# Install required Ubuntu packages
# =============================================================================

log "Installing system dependencies"

export DEBIAN_FRONTEND=noninteractive

apt-get update

apt-get install -y \
    ca-certificates \
    curl \
    wget \
    git \
    gnupg \
    lsb-release \
    openssl \
    jq \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    build-essential \
    libpq-dev \
    pkg-config \
    ufw

# =============================================================================
# Install Docker
# =============================================================================

log "Installing Docker"

if ! command -v docker >/dev/null 2>&1; then
    info "Docker is not installed. Installing Docker..."

    install -m 0755 -d /etc/apt/keyrings

    if [[ ! -f /etc/apt/keyrings/docker.asc ]]; then
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
            -o /etc/apt/keyrings/docker.asc

        chmod a+r /etc/apt/keyrings/docker.asc
    fi

    ARCH="$(dpkg --print-architecture)"

    cat > /etc/apt/sources.list.d/docker.list <<EOF
deb [arch=${ARCH} signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable
EOF

    apt-get update

    apt-get install -y \
        docker-ce \
        docker-ce-cli \
        containerd.io \
        docker-buildx-plugin \
        docker-compose-plugin
else
    info "Docker already installed."
fi

systemctl enable docker
systemctl start docker

if ! docker info >/dev/null 2>&1; then
    die "Docker daemon is not running."
fi

if ! docker compose version >/dev/null 2>&1; then
    die "Docker Compose plugin is not available."
fi

info "Docker: $(docker --version)"
info "Compose: $(${COMPOSE} version)"

# =============================================================================
# Create .env
# =============================================================================

log "Configuring environment"

generate_secret() {
    openssl rand -hex 32
}

generate_fernet_key() {
    python3 - <<'PY'
import base64
import os

print(base64.urlsafe_b64encode(os.urandom(32)).decode())
PY
}

# Preserve an existing .env.
if [[ -f "${ENV_FILE}" ]]; then
    info ".env already exists. Existing values will be preserved."
else
    info "Creating .env..."

    POSTGRES_PASSWORD="$(generate_secret)"
    INTERNAL_API_KEY="$(generate_secret)"
    ENCRYPTION_KEY="$(generate_fernet_key)"

    cat > "${ENV_FILE}" <<EOF
# =============================================================================
# VPN-3X environment
# Generated by install.sh
# =============================================================================

# -----------------------------------------------------------------------------
# PostgreSQL
# -----------------------------------------------------------------------------

POSTGRES_DB=vpn3x
POSTGRES_USER=vpn3x
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}

# -----------------------------------------------------------------------------
# Application
# -----------------------------------------------------------------------------

DATABASE_URL=postgresql+asyncpg://vpn3x:${POSTGRES_PASSWORD}@postgres:5432/vpn3x

REDIS_URL=redis://redis:6379/0

ENCRYPTION_KEY=${ENCRYPTION_KEY}

INTERNAL_API_KEY=${INTERNAL_API_KEY}

# -----------------------------------------------------------------------------
# Telegram
# -----------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN=
TELEGRAM_ADMIN_IDS=

# -----------------------------------------------------------------------------
# Server
# -----------------------------------------------------------------------------

SERVER_API_URL=http://server:8000

# -----------------------------------------------------------------------------
# Optional external integrations
# -----------------------------------------------------------------------------

CLOUDFLARE_API_TOKEN=
CLOUDFLARE_ZONE_ID=
EOF

    chmod 600 "${ENV_FILE}"
fi

# =============================================================================
# Helper for reading .env
# =============================================================================

get_env_value() {
    local key="$1"

    if [[ ! -f "${ENV_FILE}" ]]; then
        return 1
    fi

    grep -E "^${key}=" "${ENV_FILE}" \
        | tail -n1 \
        | cut -d'=' -f2-
}

set_env_value() {
    local key="$1"
    local value="$2"

    touch "${ENV_FILE}"

    if grep -qE "^${key}=" "${ENV_FILE}"; then
        sed -i "s|^${key}=.*$|${key}=${value}|" "${ENV_FILE}"
    else
        echo "${key}=${value}" >> "${ENV_FILE}"
    fi
}

# =============================================================================
# Ask for required Telegram configuration
# =============================================================================

log "Telegram configuration"

CURRENT_BOT_TOKEN="$(get_env_value TELEGRAM_BOT_TOKEN || true)"
CURRENT_ADMIN_IDS="$(get_env_value TELEGRAM_ADMIN_IDS || true)"

if [[ -z "${CURRENT_BOT_TOKEN}" ]]; then
    echo
    echo "Enter the Telegram Bot Token."
    echo "Example: 123456789:AA..."
    echo

    read -r -p "TELEGRAM_BOT_TOKEN: " TELEGRAM_BOT_TOKEN

    if [[ -z "${TELEGRAM_BOT_TOKEN}" ]]; then
        die "TELEGRAM_BOT_TOKEN cannot be empty."
    fi

    set_env_value "TELEGRAM_BOT_TOKEN" "${TELEGRAM_BOT_TOKEN}"
fi

if [[ -z "${CURRENT_ADMIN_IDS}" ]]; then
    echo
    echo "Enter Telegram administrator ID(s)."
    echo "For multiple administrators use comma-separated IDs."
    echo "Example: 123456789,987654321"
    echo

    read -r -p "TELEGRAM_ADMIN_IDS: " TELEGRAM_ADMIN_IDS

    if [[ -z "${TELEGRAM_ADMIN_IDS}" ]]; then
        die "TELEGRAM_ADMIN_IDS cannot be empty."
    fi

    set_env_value "TELEGRAM_ADMIN_IDS" "${TELEGRAM_ADMIN_IDS}"
fi

chmod 600 "${ENV_FILE}"

# =============================================================================
# Validate required .env values
# =============================================================================

log "Validating environment"

required_env=(
    "POSTGRES_DB"
    "POSTGRES_USER"
    "POSTGRES_PASSWORD"
    "DATABASE_URL"
    "REDIS_URL"
    "ENCRYPTION_KEY"
    "INTERNAL_API_KEY"
    "TELEGRAM_BOT_TOKEN"
    "TELEGRAM_ADMIN_IDS"
)

for key in "${required_env[@]}"; do
    value="$(get_env_value "${key}" || true)"

    if [[ -z "${value}" ]]; then
        die "Required environment variable ${key} is missing or empty in ${ENV_FILE}."
    fi
done

# =============================================================================
# Validate Docker Compose configuration
# =============================================================================

log "Validating Docker Compose configuration"

${COMPOSE} --env-file "${ENV_FILE}" config >/tmp/vpn3x-compose-config.yml

info "Docker Compose configuration is valid."

# =============================================================================
# Build migration virtual environment
# =============================================================================

log "Preparing Python migration environment"

if [[ ! -d "${MIGRATION_VENV}" ]]; then
    python3 -m venv "${MIGRATION_VENV}"
fi

# shellcheck disable=SC1091
source "${MIGRATION_VENV}/bin/activate"

python -m pip install --upgrade pip setuptools wheel

python -m pip install -r "${APP_DIR}/server/requirements.txt"

# Some repositories keep migration dependencies separately.
if [[ -f "${APP_DIR}/requirements.txt" ]]; then
    python -m pip install -r "${APP_DIR}/requirements.txt"
fi

deactivate

# =============================================================================
# Stop previous stack
# =============================================================================

log "Stopping previous VPN-3X stack"

${COMPOSE} --env-file "${ENV_FILE}" down --remove-orphans || true

# =============================================================================
# Pull/build images
# =============================================================================

log "Building application images"

${COMPOSE} --env-file "${ENV_FILE}" build --pull

# =============================================================================
# Start infrastructure first
# =============================================================================

log "Starting PostgreSQL and Redis"

# Try to detect common service names.
COMPOSE_SERVICES="$(${COMPOSE} --env-file "${ENV_FILE}" config --services)"

POSTGRES_SERVICE=""
REDIS_SERVICE=""

if echo "${COMPOSE_SERVICES}" | grep -qx "postgres"; then
    POSTGRES_SERVICE="postgres"
elif echo "${COMPOSE_SERVICES}" | grep -qx "db"; then
    POSTGRES_SERVICE="db"
fi

if echo "${COMPOSE_SERVICES}" | grep -qx "redis"; then
    REDIS_SERVICE="redis"
fi

if [[ -n "${POSTGRES_SERVICE}" ]]; then
    ${COMPOSE} --env-file "${ENV_FILE}" up -d "${POSTGRES_SERVICE}"
else
    warn "Could not automatically identify PostgreSQL Compose service."
fi

if [[ -n "${REDIS_SERVICE}" ]]; then
    ${COMPOSE} --env-file "${ENV_FILE}" up -d "${REDIS_SERVICE}"
else
    warn "Could not automatically identify Redis Compose service."
fi

# =============================================================================
# Wait for PostgreSQL
# =============================================================================

log "Waiting for PostgreSQL"

if [[ -n "${POSTGRES_SERVICE}" ]]; then
    POSTGRES_READY=0

    for _ in $(seq 1 60); do
        if ${COMPOSE} --env-file "${ENV_FILE}" exec -T "${POSTGRES_SERVICE}" \
            pg_isready >/dev/null 2>&1; then
            POSTGRES_READY=1
            break
        fi

        sleep 2
    done

    if [[ "${POSTGRES_READY}" -ne 1 ]]; then
        ${COMPOSE} --env-file "${ENV_FILE}" logs --tail=100 "${POSTGRES_SERVICE}" || true
        die "PostgreSQL did not become ready."
    fi

    info "PostgreSQL is ready."
fi

# =============================================================================
# Run migrations
# =============================================================================

log "Running database migrations"

cd "${APP_DIR}"

# Run migrations using the same environment expected by the application.
source "${MIGRATION_VENV}/bin/activate"

export DATABASE_URL="$(get_env_value DATABASE_URL)"
export REDIS_URL="$(get_env_value REDIS_URL)"
export ENCRYPTION_KEY="$(get_env_value ENCRYPTION_KEY)"
export INTERNAL_API_KEY="$(get_env_value INTERNAL_API_KEY)"
export TELEGRAM_BOT_TOKEN="$(get_env_value TELEGRAM_BOT_TOKEN)"
export TELEGRAM_ADMIN_IDS="$(get_env_value TELEGRAM_ADMIN_IDS)"

if [[ -d "${APP_DIR}/server" ]]; then
    export PYTHONPATH="${APP_DIR}/server${PYTHONPATH:+:${PYTHONPATH}}"
fi

cd "${APP_DIR}/migrations"

alembic -c alembic.ini upgrade head

deactivate

cd "${APP_DIR}"

info "Database migrations completed."

# =============================================================================
# Start complete stack
# =============================================================================

log "Starting VPN-3X"

${COMPOSE} --env-file "${ENV_FILE}" up -d

# =============================================================================
# Wait for application health
# =============================================================================

log "Waiting for API"

HEALTH_URL="http://127.0.0.1:8000/health"

API_READY=0

for _ in $(seq 1 60); do
    if curl -fsS "${HEALTH_URL}" >/dev/null 2>&1; then
        API_READY=1
        break
    fi

    sleep 2
done

if [[ "${API_READY}" -ne 1 ]]; then
    warn "API did not become healthy within the expected time."

    echo
    echo "Container status:"
    ${COMPOSE} --env-file "${ENV_FILE}" ps || true

    echo
    echo "Recent logs:"
    ${COMPOSE} --env-file "${ENV_FILE}" logs --tail=100 || true

    die "VPN-3X API health check failed."
fi

info "API is healthy: ${HEALTH_URL}"

# =============================================================================
# Install systemd service
# =============================================================================

log "Installing systemd service"

cat > /etc/systemd/system/vpn-3x.service <<EOF
[Unit]
Description=VPN-3X Docker Stack
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes

WorkingDirectory=${APP_DIR}

ExecStart=/usr/bin/docker compose --env-file ${ENV_FILE} up -d
ExecStop=/usr/bin/docker compose --env-file ${ENV_FILE} down

TimeoutStartSec=0
TimeoutStopSec=120

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable vpn-3x.service

# Start once through systemd so the service state is correct.
systemctl start vpn-3x.service || true

# =============================================================================
# Optional firewall
# =============================================================================

log "Firewall"

if command -v ufw >/dev/null 2>&1; then
    info "UFW is installed."

    # Do not enable UFW automatically.
    # The VPN node ports and deployment-specific ports are not known here.
    info "UFW was NOT enabled automatically."
    info "Configure firewall rules according to your deployment."
fi

# =============================================================================
# Final verification
# =============================================================================

log "Final verification"

echo
echo "Docker:"
docker --version

echo
echo "Docker Compose:"
${COMPOSE} version

echo
echo "Systemd:"
systemctl is-enabled vpn-3x.service || true
systemctl is-active vpn-3x.service || true

echo
echo "Containers:"
${COMPOSE} --env-file "${ENV_FILE}" ps

echo
echo "API health:"
curl -fsS "${HEALTH_URL}" || die "Final API health check failed."

echo
echo

# =============================================================================
# Installation complete
# =============================================================================

log "VPN-3X installation completed successfully"

cat <<EOF

Installation directory:
  ${APP_DIR}

Environment:
  ${ENV_FILE}

Migration virtualenv:
  ${MIGRATION_VENV}

API:
  ${HEALTH_URL}

Systemd:
  vpn-3x.service

Useful commands:

  cd ${APP_DIR}

  # Container status
  docker compose ps

  # All logs
  docker compose logs -f

  # Server logs
  docker compose logs -f server

  # Worker logs
  docker compose logs -f worker

  # Bot logs
  docker compose logs -f bot

  # Restart
  systemctl restart vpn-3x.service

  # Stop
  systemctl stop vpn-3x.service

  # Start
  systemctl start vpn-3x.service

  # Service status
  systemctl status vpn-3x.service

  # API health
  curl http://127.0.0.1:8000/health

IMPORTANT:
  ${ENV_FILE} contains secrets.
  Keep its permissions restricted and do not commit it to Git.

EOF