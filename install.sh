#!/usr/bin/env bash
set -Eeuo pipefail

# =============================================================================
# VPN-3X Ubuntu Installer
# =============================================================================
#
# Supported:
#   Ubuntu 22.04 LTS
#   Ubuntu 24.04 LTS
#
# Run:
#   sudo ./install.sh
#
# Expected repository:
#
#   VPN-3X/
#   ├── install.sh
#   ├── docker-compose.yml
#   ├── server/
#   │   ├── Dockerfile
#   │   ├── requirements.txt
#   │   └── app/
#   ├── bot/
#   │   ├── Dockerfile
#   │   └── ...
#   └── migrations/
#       ├── alembic.ini
#       ├── env.py
#       └── versions/
#
# =============================================================================

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${APP_DIR}/.env"
COMPOSE_FILE="${APP_DIR}/docker-compose.yml"

POSTGRES_SERVICE="db"
REDIS_SERVICE="redis"
SERVER_SERVICE="server"
WORKER_SERVICE="worker"
BOT_SERVICE="bot"

HEALTH_URL="http://127.0.0.1:8000/health"

export DEBIAN_FRONTEND=noninteractive


# =============================================================================
# Helpers
# =============================================================================

log() {
    echo
    echo "================================================================"
    echo "$1"
    echo "================================================================"
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

on_error() {
    local exit_code=$?

    echo
    echo "================================================================"
    echo "VPN-3X INSTALLATION FAILED"
    echo "================================================================"
    echo
    echo "Exit code: ${exit_code}"
    echo
    echo "Diagnostics:"
    echo
    echo "  cd ${APP_DIR}"
    echo "  docker compose ps"
    echo "  docker compose logs --tail=150"
    echo
    echo "Systemd:"
    echo "  systemctl status vpn-3x.service"
    echo

    exit "${exit_code}"
}

trap on_error ERR


# =============================================================================
# Root
# =============================================================================

if [[ "${EUID}" -ne 0 ]]; then
    die "Run as root: sudo ./install.sh"
fi


# =============================================================================
# Repository validation
# =============================================================================

log "Checking repository"

[[ -f "${COMPOSE_FILE}" ]] \
    || die "docker-compose.yml not found."

[[ -f "${APP_DIR}/server/Dockerfile" ]] \
    || die "server/Dockerfile not found."

[[ -f "${APP_DIR}/server/requirements.txt" ]] \
    || die "server/requirements.txt not found."

[[ -f "${APP_DIR}/bot/Dockerfile" ]] \
    || die "bot/Dockerfile not found."

[[ -f "${APP_DIR}/migrations/alembic.ini" ]] \
    || die "migrations/alembic.ini not found."

[[ -f "${APP_DIR}/migrations/env.py" ]] \
    || die "migrations/env.py not found."

[[ -d "${APP_DIR}/migrations/versions" ]] \
    || die "migrations/versions directory not found."

cd "${APP_DIR}"

info "Repository: ${APP_DIR}"


# =============================================================================
# OS validation
# =============================================================================

log "Checking operating system"

[[ -f /etc/os-release ]] \
    || die "/etc/os-release not found."

source /etc/os-release

if [[ "${ID:-}" != "ubuntu" ]]; then
    die "Ubuntu is required. Detected: ${PRETTY_NAME:-unknown}"
fi

case "${VERSION_ID:-}" in
    22.04|24.04)
        info "Ubuntu ${VERSION_ID} detected."
        ;;
    *)
        warn "Ubuntu ${VERSION_ID:-unknown} detected."
        warn "Primary supported versions are 22.04 and 24.04."
        ;;
esac


# =============================================================================
# System dependencies
# =============================================================================

log "Installing system dependencies"

apt-get update

apt-get install -y \
    ca-certificates \
    curl \
    git \
    gnupg \
    openssl \
    jq \
    lsb-release \
    apt-transport-https


# =============================================================================
# Docker
# =============================================================================

log "Installing Docker"

if ! command -v docker >/dev/null 2>&1; then

    info "Docker not found. Installing..."

    install -m 0755 -d /etc/apt/keyrings

    if [[ ! -f /etc/apt/keyrings/docker.asc ]]; then
        curl -fsSL \
            https://download.docker.com/linux/ubuntu/gpg \
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

systemctl is-active --quiet docker \
    || die "Docker daemon is not running."

docker info >/dev/null 2>&1 \
    || die "Cannot communicate with Docker daemon."

docker compose version >/dev/null 2>&1 \
    || die "Docker Compose plugin is unavailable."

info "$(docker --version)"
info "$(docker compose version)"


# =============================================================================
# Environment functions
# =============================================================================

get_env() {
    local key="$1"

    [[ -f "${ENV_FILE}" ]] || return 1

    grep -E "^${key}=" "${ENV_FILE}" \
        | tail -n 1 \
        | cut -d '=' -f 2-
}


set_env() {
    local key="$1"
    local value="$2"

    if grep -qE "^${key}=" "${ENV_FILE}"; then
        sed -i "s|^${key}=.*$|${key}=${value}|" "${ENV_FILE}"
    else
        echo "${key}=${value}" >> "${ENV_FILE}"
    fi
}


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


# =============================================================================
# .env
# =============================================================================

log "Configuring environment"

if [[ ! -f "${ENV_FILE}" ]]; then

    info "Creating ${ENV_FILE}"

    POSTGRES_PASSWORD="$(generate_secret)"
    INTERNAL_API_KEY="$(generate_secret)"
    ENCRYPTION_KEY="$(generate_fernet_key)"

    cat > "${ENV_FILE}" <<EOF
# =============================================================================
# VPN-3X
# Generated automatically by install.sh
# =============================================================================

POSTGRES_USER=vpn3x
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
POSTGRES_DB=vpn3x

DATABASE_URL=postgresql+asyncpg://vpn3x:${POSTGRES_PASSWORD}@db:5432/vpn3x

REDIS_URL=redis://redis:6379/0

ENCRYPTION_KEY=${ENCRYPTION_KEY}

INTERNAL_API_KEY=${INTERNAL_API_KEY}

TELEGRAM_BOT_TOKEN=
TELEGRAM_ADMIN_IDS=
EOF

    chmod 600 "${ENV_FILE}"

else
    info "${ENV_FILE} already exists."
    info "Existing secrets will be preserved."
fi


# =============================================================================
# Database configuration
# =============================================================================

log "Configuring database"

POSTGRES_USER="$(get_env POSTGRES_USER || true)"
POSTGRES_PASSWORD="$(get_env POSTGRES_PASSWORD || true)"
POSTGRES_DB="$(get_env POSTGRES_DB || true)"

[[ -n "${POSTGRES_USER}" ]] \
    || set_env POSTGRES_USER "vpn3x"

[[ -n "${POSTGRES_DB}" ]] \
    || set_env POSTGRES_DB "vpn3x"

if [[ -z "${POSTGRES_PASSWORD}" ]]; then
    POSTGRES_PASSWORD="$(generate_secret)"
    set_env POSTGRES_PASSWORD "${POSTGRES_PASSWORD}"
fi

POSTGRES_USER="$(get_env POSTGRES_USER)"
POSTGRES_PASSWORD="$(get_env POSTGRES_PASSWORD)"
POSTGRES_DB="$(get_env POSTGRES_DB)"


# =============================================================================
# Application secrets
# =============================================================================

INTERNAL_API_KEY="$(get_env INTERNAL_API_KEY || true)"

if [[ -z "${INTERNAL_API_KEY}" ]]; then
    INTERNAL_API_KEY="$(generate_secret)"
    set_env INTERNAL_API_KEY "${INTERNAL_API_KEY}"
fi


ENCRYPTION_KEY="$(get_env ENCRYPTION_KEY || true)"

if [[ -z "${ENCRYPTION_KEY}" ]]; then
    ENCRYPTION_KEY="$(generate_fernet_key)"
    set_env ENCRYPTION_KEY "${ENCRYPTION_KEY}"
fi


# =============================================================================
# Internal Docker URLs
# =============================================================================

set_env \
    DATABASE_URL \
    "postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB}"

set_env \
    REDIS_URL \
    "redis://redis:6379/0"


# =============================================================================
# Telegram
# =============================================================================

log "Telegram configuration"

TELEGRAM_BOT_TOKEN="$(get_env TELEGRAM_BOT_TOKEN || true)"
TELEGRAM_ADMIN_IDS="$(get_env TELEGRAM_ADMIN_IDS || true)"


if [[ -z "${TELEGRAM_BOT_TOKEN}" ]]; then

    echo
    echo "Enter Telegram Bot Token."
    echo

    read -r -p "TELEGRAM_BOT_TOKEN: " TELEGRAM_BOT_TOKEN

    [[ -n "${TELEGRAM_BOT_TOKEN}" ]] \
        || die "TELEGRAM_BOT_TOKEN cannot be empty."

    set_env TELEGRAM_BOT_TOKEN "${TELEGRAM_BOT_TOKEN}"
fi


if [[ -z "${TELEGRAM_ADMIN_IDS}" ]]; then

    echo
    echo "Enter Telegram administrator ID(s)."
    echo "Multiple IDs: 123456789,987654321"
    echo

    read -r -p "TELEGRAM_ADMIN_IDS: " TELEGRAM_ADMIN_IDS

    [[ -n "${TELEGRAM_ADMIN_IDS}" ]] \
        || die "TELEGRAM_ADMIN_IDS cannot be empty."

    set_env TELEGRAM_ADMIN_IDS "${TELEGRAM_ADMIN_IDS}"
fi


chmod 600 "${ENV_FILE}"


# =============================================================================
# Validate Compose
# =============================================================================

log "Validating Docker Compose"

docker compose \
    --env-file "${ENV_FILE}" \
    -f "${COMPOSE_FILE}" \
    config >/dev/null

info "Compose configuration is valid."


# =============================================================================
# Stop old installation
# =============================================================================

log "Stopping existing VPN-3X stack"

docker compose \
    --env-file "${ENV_FILE}" \
    -f "${COMPOSE_FILE}" \
    down --remove-orphans || true


# =============================================================================
# Pull infrastructure
# =============================================================================

log "Pulling PostgreSQL and Redis"

docker compose \
    --env-file "${ENV_FILE}" \
    -f "${COMPOSE_FILE}" \
    pull \
    "${POSTGRES_SERVICE}" \
    "${REDIS_SERVICE}"


# =============================================================================
# Build application
# =============================================================================

log "Building application containers"

docker compose \
    --env-file "${ENV_FILE}" \
    -f "${COMPOSE_FILE}" \
    build --pull \
    "${SERVER_SERVICE}" \
    "${WORKER_SERVICE}" \
    "${BOT_SERVICE}"


# =============================================================================
# Start database and Redis
# =============================================================================

log "Starting PostgreSQL and Redis"

docker compose \
    --env-file "${ENV_FILE}" \
    -f "${COMPOSE_FILE}" \
    up -d \
    "${POSTGRES_SERVICE}" \
    "${REDIS_SERVICE}"


# =============================================================================
# Wait for PostgreSQL
# =============================================================================

log "Waiting for PostgreSQL"

POSTGRES_READY=0

for i in $(seq 1 60); do

    if docker compose \
        --env-file "${ENV_FILE}" \
        -f "${COMPOSE_FILE}" \
        exec -T "${POSTGRES_SERVICE}" \
        pg_isready \
        -U "${POSTGRES_USER}" \
        -d "${POSTGRES_DB}" \
        >/dev/null 2>&1
    then
        POSTGRES_READY=1
        break
    fi

    info "Waiting for PostgreSQL (${i}/60)..."
    sleep 2
done

[[ "${POSTGRES_READY}" -eq 1 ]] \
    || die "PostgreSQL did not become ready."


# =============================================================================
# Build server image
#
# This image contains:
#   Python 3.12
#   server requirements
#   app/
#
# It will also be reused for migrations.
# =============================================================================

log "Starting server"

docker compose \
    --env-file "${ENV_FILE}" \
    -f "${COMPOSE_FILE}" \
    up -d \
    "${SERVER_SERVICE}"


# =============================================================================
# Wait for server
# =============================================================================

log "Waiting for server"

for i in $(seq 1 60); do

    SERVER_CONTAINER="$(
        docker compose \
            --env-file "${ENV_FILE}" \
            -f "${COMPOSE_FILE}" \
            ps -q "${SERVER_SERVICE}"
    )"

    if [[ -n "${SERVER_CONTAINER}" ]]; then

        STATUS="$(
            docker inspect \
                --format '{{.State.Status}}' \
                "${SERVER_CONTAINER}" \
                2>/dev/null || true
        )

        if [[ "${STATUS}" == "running" ]]; then
            break
        fi
    fi

    info "Waiting for server container (${i}/60)..."
    sleep 2

done


# =============================================================================
# Run migrations
#
# migrations/ is mounted into the server container.
#
# Container:
#
#   /app       -> server/
#   /migrations -> migrations/
#
# migrations/env.py adds ../server to sys.path, so app imports work.
# =============================================================================

log "Running Alembic migrations"

docker compose \
    --env-file "${ENV_FILE}" \
    -f "${COMPOSE_FILE}" \
    run \
    --rm \
    --no-deps \
    -v "${APP_DIR}/migrations:/migrations:ro" \
    "${SERVER_SERVICE}" \
    alembic \
    -c /migrations/alembic.ini \
    upgrade head


# =============================================================================
# Start worker and bot
# =============================================================================

log "Starting worker and bot"

docker compose \
    --env-file "${ENV_FILE}" \
    -f "${COMPOSE_FILE}" \
    up -d \
    "${WORKER_SERVICE}" \
    "${BOT_SERVICE}"


# =============================================================================
# Wait for API
# =============================================================================

log "Checking API"

API_READY=0

for i in $(seq 1 60); do

    if curl \
        --silent \
        --show-error \
        --fail \
        "${HEALTH_URL}" \
        >/dev/null 2>&1
    then
        API_READY=1
        break
    fi

    info "Waiting for API (${i}/60)..."
    sleep 2
done


if [[ "${API_READY}" -ne 1 ]]; then

    echo
    echo "================ SERVER LOGS ================"
    docker compose \
        --env-file "${ENV_FILE}" \
        -f "${COMPOSE_FILE}" \
        logs --tail=150 "${SERVER_SERVICE}" || true

    echo
    echo "================ ALL SERVICES ================"
    docker compose \
        --env-file "${ENV_FILE}" \
        -f "${COMPOSE_FILE}" \
        ps || true

    die "API health check failed."
fi


# =============================================================================
# Check worker
# =============================================================================

log "Checking worker"

WORKER_CONTAINER="$(
    docker compose \
        --env-file "${ENV_FILE}" \
        -f "${COMPOSE_FILE}" \
        ps -q "${WORKER_SERVICE}"
)"

if [[ -z "${WORKER_CONTAINER}" ]]; then
    die "Worker container was not created."
fi

WORKER_STATUS="$(
    docker inspect \
        --format '{{.State.Status}}' \
        "${WORKER_CONTAINER}"
)

if [[ "${WORKER_STATUS}" != "running" ]]; then
    docker compose \
        --env-file "${ENV_FILE}" \
        -f "${COMPOSE_FILE}" \
        logs --tail=100 "${WORKER_SERVICE}" || true

    die "Worker is not running."
fi


# =============================================================================
# Check bot
# =============================================================================

log "Checking Telegram bot"

BOT_CONTAINER="$(
    docker compose \
        --env-file "${ENV_FILE}" \
        -f "${COMPOSE_FILE}" \
        ps -q "${BOT_SERVICE}"
)"

if [[ -z "${BOT_CONTAINER}" ]]; then
    die "Bot container was not created."
fi

BOT_STATUS="$(
    docker inspect \
        --format '{{.State.Status}}' \
        "${BOT_CONTAINER}"
)

if [[ "${BOT_STATUS}" != "running" ]]; then
    docker compose \
        --env-file "${ENV_FILE}" \
        -f "${COMPOSE_FILE}" \
        logs --tail=100 "${BOT_SERVICE}" || true

    die "Bot is not running."
fi


# =============================================================================
# Systemd
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

ExecStart=/usr/bin/docker compose --env-file ${ENV_FILE} -f ${COMPOSE_FILE} up -d
ExecStop=/usr/bin/docker compose --env-file ${ENV_FILE} -f ${COMPOSE_FILE} down

TimeoutStartSec=0
TimeoutStopSec=120

[Install]
WantedBy=multi-user.target
EOF


systemctl daemon-reload
systemctl enable vpn-3x.service


# =============================================================================
# Final verification
# =============================================================================

log "Final verification"

echo
echo "Docker containers:"
docker compose \
    --env-file "${ENV_FILE}" \
    -f "${COMPOSE_FILE}" \
    ps

echo
echo "API:"
curl -fsS "${HEALTH_URL}"

echo
echo
echo "Systemd:"
systemctl is-enabled vpn-3x.service

echo
echo "================================================================"
echo "VPN-3X INSTALLED SUCCESSFULLY"
echo "================================================================"
echo
echo "Directory:"
echo "  ${APP_DIR}"
echo
echo "Environment:"
echo "  ${ENV_FILE}"
echo
echo "API:"
echo "  ${HEALTH_URL}"
echo
echo "Service:"
echo "  vpn-3x.service"
echo
echo "Useful commands:"
echo
echo "  cd ${APP_DIR}"
echo
echo "  docker compose ps"
echo
echo "  docker compose logs -f"
echo
echo "  docker compose logs -f server"
echo
echo "  docker compose logs -f worker"
echo
echo "  docker compose logs -f bot"
echo
echo "  systemctl status vpn-3x.service"
echo
echo "  systemctl restart vpn-3x.service"
echo
echo "  systemctl stop vpn-3x.service"
echo
echo "  systemctl start vpn-3x.service"
echo
echo "  curl ${HEALTH_URL}"
echo
echo "IMPORTANT:"
echo "  ${ENV_FILE} contains secrets."
echo "  Never commit .env to Git."
echo