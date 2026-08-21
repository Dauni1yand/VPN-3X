#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${APP_DIR}/.env"
COMPOSE_FILE="${APP_DIR}/docker-compose.yml"

log() {
    echo
    echo "============================================================"
    echo "$1"
    echo "============================================================"
}

die() {
    echo "[ERROR] $1"
    exit 1
}

# ------------------------------------------------------------
# Root
# ------------------------------------------------------------

if [[ "$EUID" -ne 0 ]]; then
    die "Запусти скрипт через sudo: sudo ./install.sh"
fi

cd "$APP_DIR"

# ------------------------------------------------------------
# Check repository
# ------------------------------------------------------------

log "Проверка проекта"

[[ -f "$COMPOSE_FILE" ]] || die "Не найден docker-compose.yml"
[[ -f "$APP_DIR/server/Dockerfile" ]] || die "Не найден server/Dockerfile"
[[ -f "$APP_DIR/server/requirements.txt" ]] || die "Не найден server/requirements.txt"
[[ -f "$APP_DIR/bot/Dockerfile" ]] || die "Не найден bot/Dockerfile"
[[ -f "$APP_DIR/migrations/alembic.ini" ]] || die "Не найден migrations/alembic.ini"
[[ -f "$APP_DIR/migrations/env.py" ]] || die "Не найден migrations/env.py"

# ------------------------------------------------------------
# Ubuntu
# ------------------------------------------------------------

log "Проверка Ubuntu"

source /etc/os-release

if [[ "${ID:-}" != "ubuntu" ]]; then
    die "Этот installer предназначен для Ubuntu. Обнаружено: ${PRETTY_NAME:-unknown}"
fi

echo "Ubuntu ${VERSION_ID}"

# ------------------------------------------------------------
# System packages
# ------------------------------------------------------------

log "Установка системных зависимостей"

apt-get update

apt-get install -y \
    ca-certificates \
    curl \
    git \
    gnupg \
    openssl \
    python3 \
    python3-pip \
    python3-venv \
    lsb-release

# ------------------------------------------------------------
# Docker
# ------------------------------------------------------------

log "Проверка Docker"

if ! command -v docker >/dev/null 2>&1; then

    echo "Docker не установлен. Устанавливаю..."

    install -m 0755 -d /etc/apt/keyrings

    curl -fsSL \
        https://download.docker.com/linux/ubuntu/gpg \
        -o /etc/apt/keyrings/docker.asc

    chmod a+r /etc/apt/keyrings/docker.asc

    ARCH="$(dpkg --print-architecture)"

    echo \
        "deb [arch=${ARCH} signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
        > /etc/apt/sources.list.d/docker.list

    apt-get update

    apt-get install -y \
        docker-ce \
        docker-ce-cli \
        containerd.io \
        docker-buildx-plugin \
        docker-compose-plugin
fi

systemctl enable docker
systemctl start docker

docker info >/dev/null 2>&1 || die "Docker daemon не запущен"

docker compose version >/dev/null 2>&1 \
    || die "Docker Compose plugin не найден"

echo "Docker: $(docker --version)"
echo "Compose: $(docker compose version)"

# ------------------------------------------------------------
# Environment helpers
# ------------------------------------------------------------

get_env() {
    local key="$1"

    if [[ ! -f "$ENV_FILE" ]]; then
        return 1
    fi

    grep -E "^${key}=" "$ENV_FILE" \
        | tail -n 1 \
        | cut -d '=' -f 2-
}

set_env() {
    local key="$1"
    local value="$2"

    if grep -qE "^${key}=" "$ENV_FILE"; then
        sed -i "s|^${key}=.*$|${key}=${value}|" "$ENV_FILE"
    else
        echo "${key}=${value}" >> "$ENV_FILE"
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

# ------------------------------------------------------------
# .env
# ------------------------------------------------------------

log "Настройка .env"

if [[ ! -f "$ENV_FILE" ]]; then

    POSTGRES_PASSWORD="$(generate_secret)"
    INTERNAL_API_KEY="$(generate_secret)"
    ENCRYPTION_KEY="$(generate_fernet_key)"

    cat > "$ENV_FILE" <<EOF
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

    chmod 600 "$ENV_FILE"

    echo ".env создан"

else

    echo ".env уже существует — существующие значения сохраняются"

fi

# ------------------------------------------------------------
# Required environment
# ------------------------------------------------------------

log "Проверка переменных окружения"

POSTGRES_USER="$(get_env POSTGRES_USER || true)"
POSTGRES_PASSWORD="$(get_env POSTGRES_PASSWORD || true)"
POSTGRES_DB="$(get_env POSTGRES_DB || true)"

if [[ -z "$POSTGRES_USER" ]]; then
    set_env POSTGRES_USER "vpn3x"
    POSTGRES_USER="vpn3x"
fi

if [[ -z "$POSTGRES_DB" ]]; then
    set_env POSTGRES_DB "vpn3x"
    POSTGRES_DB="vpn3x"
fi

if [[ -z "$POSTGRES_PASSWORD" ]]; then
    POSTGRES_PASSWORD="$(generate_secret)"
    set_env POSTGRES_PASSWORD "$POSTGRES_PASSWORD"
fi

set_env \
    DATABASE_URL \
    "postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB}"

set_env REDIS_URL "redis://redis:6379/0"

if [[ -z "$(get_env ENCRYPTION_KEY || true)" ]]; then
    set_env ENCRYPTION_KEY "$(generate_fernet_key)"
fi

if [[ -z "$(get_env INTERNAL_API_KEY || true)" ]]; then
    set_env INTERNAL_API_KEY "$(generate_secret)"
fi

# ------------------------------------------------------------
# Telegram
# ------------------------------------------------------------

log "Настройка Telegram"

TELEGRAM_BOT_TOKEN="$(get_env TELEGRAM_BOT_TOKEN || true)"
TELEGRAM_ADMIN_IDS="$(get_env TELEGRAM_ADMIN_IDS || true)"

if [[ -z "$TELEGRAM_BOT_TOKEN" ]]; then

    read -r -p "Введите TELEGRAM_BOT_TOKEN: " TELEGRAM_BOT_TOKEN

    [[ -n "$TELEGRAM_BOT_TOKEN" ]] \
        || die "TELEGRAM_BOT_TOKEN не может быть пустым"

    set_env TELEGRAM_BOT_TOKEN "$TELEGRAM_BOT_TOKEN"
fi

if [[ -z "$TELEGRAM_ADMIN_IDS" ]]; then

    read -r -p "Введите TELEGRAM_ADMIN_IDS: " TELEGRAM_ADMIN_IDS

    [[ -n "$TELEGRAM_ADMIN_IDS" ]] \
        || die "TELEGRAM_ADMIN_IDS не может быть пустым"

    set_env TELEGRAM_ADMIN_IDS "$TELEGRAM_ADMIN_IDS"
fi

chmod 600 "$ENV_FILE"

# ------------------------------------------------------------
# Validate Compose
# ------------------------------------------------------------

log "Проверка Docker Compose"

docker compose \
    --env-file "$ENV_FILE" \
    -f "$COMPOSE_FILE" \
    config >/dev/null

echo "docker-compose.yml корректен"

# ------------------------------------------------------------
# Stop old containers
# ------------------------------------------------------------

log "Остановка старого контейнерного стека"

docker compose \
    --env-file "$ENV_FILE" \
    -f "$COMPOSE_FILE" \
    down --remove-orphans || true

# ------------------------------------------------------------
# Pull images
# ------------------------------------------------------------

log "Загрузка PostgreSQL и Redis"

docker compose \
    --env-file "$ENV_FILE" \
    -f "$COMPOSE_FILE" \
    pull db redis

# ------------------------------------------------------------
# Build application
# ------------------------------------------------------------

log "Сборка server / worker / bot"

docker compose \
    --env-file "$ENV_FILE" \
    -f "$COMPOSE_FILE" \
    build --pull server worker bot

# ------------------------------------------------------------
# Start database
# ------------------------------------------------------------

log "Запуск PostgreSQL и Redis"

docker compose \
    --env-file "$ENV_FILE" \
    -f "$COMPOSE_FILE" \
    up -d db redis

# ------------------------------------------------------------
# Wait for PostgreSQL
# ------------------------------------------------------------

log "Ожидание PostgreSQL"

POSTGRES_READY=0

for i in $(seq 1 60); do

    if docker compose \
        --env-file "$ENV_FILE" \
        -f "$COMPOSE_FILE" \
        exec -T db \
        pg_isready \
        -U "$POSTGRES_USER" \
        -d "$POSTGRES_DB" \
        >/dev/null 2>&1
    then
        POSTGRES_READY=1
        break
    fi

    echo "PostgreSQL ещё не готов (${i}/60)"
    sleep 2
done

if [[ "$POSTGRES_READY" -ne 1 ]]; then

    docker compose \
        --env-file "$ENV_FILE" \
        -f "$COMPOSE_FILE" \
        logs --tail=100 db

    die "PostgreSQL не запустился"
fi

echo "PostgreSQL готов"

# ------------------------------------------------------------
# Start server
# ------------------------------------------------------------

log "Запуск API server"

docker compose \
    --env-file "$ENV_FILE" \
    -f "$COMPOSE_FILE" \
    up -d server

# ------------------------------------------------------------
# Wait for server container
# ------------------------------------------------------------

sleep 5

SERVER_CONTAINER="$(
    docker compose \
        --env-file "$ENV_FILE" \
        -f "$COMPOSE_FILE" \
        ps -q server
)"

if [[ -z "$SERVER_CONTAINER" ]]; then
    die "Контейнер server не создан"
fi

SERVER_STATUS="$(
    docker inspect \
        --format '{{.State.Status}}' \
        "$SERVER_CONTAINER"
)"

if [[ "$SERVER_STATUS" != "running" ]]; then

    docker compose \
        --env-file "$ENV_FILE" \
        -f "$COMPOSE_FILE" \
        logs --tail=150 server

    die "Контейнер server не запущен"
fi

# ------------------------------------------------------------
# Alembic migrations
# ------------------------------------------------------------

log "Запуск миграций базы данных"

docker compose \
    --env-file "$ENV_FILE" \
    -f "$COMPOSE_FILE" \
    run \
    --rm \
    --no-deps \
    -v "${APP_DIR}:/workspace:ro" \
    server \
    sh -c 'cd /workspace/migrations && alembic -c alembic.ini upgrade head'

echo "Миграции успешно выполнены"

# ------------------------------------------------------------
# Start worker and bot
# ------------------------------------------------------------

log "Запуск worker и bot"

docker compose \
    --env-file "$ENV_FILE" \
    -f "$COMPOSE_FILE" \
    up -d worker bot

# ------------------------------------------------------------
# API health check
# ------------------------------------------------------------

log "Проверка API"

API_READY=0

for i in $(seq 1 60); do

    if curl \
        --silent \
        --show-error \
        --fail \
        http://127.0.0.1:8000/health \
        >/dev/null 2>&1
    then
        API_READY=1
        break
    fi

    echo "API ещё не готов (${i}/60)"
    sleep 2
done

if [[ "$API_READY" -ne 1 ]]; then

    echo
    echo "================ SERVER LOGS ================"

    docker compose \
        --env-file "$ENV_FILE" \
        -f "$COMPOSE_FILE" \
        logs --tail=150 server

    die "API health check не пройден"
fi

echo "API работает"

# ------------------------------------------------------------
# Check worker
# ------------------------------------------------------------

log "Проверка worker"

WORKER_CONTAINER="$(
    docker compose \
        --env-file "$ENV_FILE" \
        -f "$COMPOSE_FILE" \
        ps -q worker
)"

[[ -n "$WORKER_CONTAINER" ]] \
    || die "Worker контейнер не создан"

WORKER_STATUS="$(
    docker inspect \
        --format '{{.State.Status}}' \
        "$WORKER_CONTAINER"
)"

if [[ "$WORKER_STATUS" != "running" ]]; then

    docker compose \
        --env-file "$ENV_FILE" \
        -f "$COMPOSE_FILE" \
        logs --tail=100 worker

    die "Worker не запущен"
fi

# ------------------------------------------------------------
# Check bot
# ------------------------------------------------------------

log "Проверка bot"

BOT_CONTAINER="$(
    docker compose \
        --env-file "$ENV_FILE" \
        -f "$COMPOSE_FILE" \
        ps -q bot
)"

[[ -n "$BOT_CONTAINER" ]] \
    || die "Bot контейнер не создан"

BOT_STATUS="$(
    docker inspect \
        --format '{{.State.Status}}' \
        "$BOT_CONTAINER"
)"

if [[ "$BOT_STATUS" != "running" ]]; then

    docker compose \
        --env-file "$ENV_FILE" \
        -f "$COMPOSE_FILE" \
        logs --tail=100 bot

    die "Bot не запущен"
fi

# ------------------------------------------------------------
# Systemd
# ------------------------------------------------------------

log "Настройка автозапуска"

cat > /etc/systemd/system/vpn-3x.service <<EOF
[Unit]
Description=VPN-3X
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

# ------------------------------------------------------------
# Final status
# ------------------------------------------------------------

log "Финальная проверка"

docker compose \
    --env-file "$ENV_FILE" \
    -f "$COMPOSE_FILE" \
    ps

echo
echo "API:"
curl -fsS http://127.0.0.1:8000/health

echo
echo
echo "============================================================"
echo "VPN-3X УСПЕШНО УСТАНОВЛЕН"
echo "============================================================"
echo
echo "Каталог:"
echo "  $APP_DIR"
echo
echo "Конфигурация:"
echo "  $ENV_FILE"
echo
echo "API:"
echo "  http://127.0.0.1:8000/health"
echo
echo "Systemd:"
echo "  vpn-3x.service"
echo
echo "Команды:"
echo
echo "  cd $APP_DIR"
echo "  docker compose ps"
echo "  docker compose logs -f"
echo "  docker compose logs -f server"
echo "  docker compose logs -f worker"
echo "  docker compose logs -f bot"
echo
echo "  systemctl status vpn-3x.service"
echo "  systemctl restart vpn-3x.service"
echo