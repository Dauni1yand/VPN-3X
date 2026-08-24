#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${APP_DIR}/.env"
COMPOSE_FILE="${APP_DIR}/docker-compose.yml"
# Set once we know whether this deployment runs the Remnawave panel itself.
# Every command that brings the stack up needs it, including the systemd
# unit -- without it the panel is simply absent after a reboot.
COMPOSE_PROFILE_ARGS=""

log() {
    echo
    echo "============================================================"
    echo "$1"
    echo "============================================================"
}

die() {
    echo
    echo "[ERROR] $1"
    exit 1
}

cleanup_on_error() {
    local code=$?
    echo
    echo "============================================================"
    echo "УСТАНОВКА ЗАВЕРШИЛАСЬ С ОШИБКОЙ"
    echo "============================================================"
    echo
    echo "Код ошибки: $code"
    echo
    echo "Для диагностики:"
    echo "  cd $APP_DIR"
    echo "  docker compose ps"
    echo "  docker compose logs --tail=150"
    echo
    exit "$code"
}

trap cleanup_on_error ERR


# ============================================================
# Root
# ============================================================

if [[ "$EUID" -ne 0 ]]; then
    die "Запусти установщик через sudo: sudo ./install.sh"
fi

cd "$APP_DIR"


# ============================================================
# Check project
# ============================================================

log "Проверка структуры проекта"

[[ -f "$COMPOSE_FILE" ]] \
    || die "Не найден docker-compose.yml"

[[ -f "$APP_DIR/server/Dockerfile" ]] \
    || die "Не найден server/Dockerfile"

[[ -f "$APP_DIR/server/requirements.txt" ]] \
    || die "Не найден server/requirements.txt"

[[ -f "$APP_DIR/bot/Dockerfile" ]] \
    || die "Не найден bot/Dockerfile"

[[ -f "$APP_DIR/migrations/alembic.ini" ]] \
    || die "Не найден migrations/alembic.ini"

[[ -f "$APP_DIR/migrations/env.py" ]] \
    || die "Не найден migrations/env.py"

echo "Структура проекта OK"


# ============================================================
# Ubuntu
# ============================================================

log "Проверка операционной системы"

if [[ ! -f /etc/os-release ]]; then
    die "Не найден /etc/os-release"
fi

source /etc/os-release

if [[ "${ID:-}" != "ubuntu" ]]; then
    die "Требуется Ubuntu. Обнаружено: ${PRETTY_NAME:-unknown}"
fi

echo "Обнаружена Ubuntu ${VERSION_ID}"


# ============================================================
# System dependencies
# ============================================================

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


# ============================================================
# Docker
# ============================================================

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

else
    echo "Docker уже установлен"
fi


systemctl enable docker
systemctl start docker

docker info >/dev/null 2>&1 \
    || die "Docker daemon недоступен"

docker compose version >/dev/null 2>&1 \
    || die "Docker Compose plugin недоступен"

echo "Docker: $(docker --version)"
echo "Compose: $(docker compose version)"


# ============================================================
# Environment helpers
# ============================================================

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


# ============================================================
# Create / preserve .env
# ============================================================

log "Настройка .env"

if [[ ! -f "$ENV_FILE" ]]; then

    POSTGRES_PASSWORD="$(generate_secret)"
    INTERNAL_API_KEY="$(generate_secret)"
    ENCRYPTION_KEY="$(generate_fernet_key)"

    cat > "$ENV_FILE" <<EOF
# ============================================================
# VPN-3X
# Generated by install.sh
# ============================================================

# PostgreSQL
POSTGRES_USER=vpn3x
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
POSTGRES_DB=vpn3x
POSTGRES_HOST=db
POSTGRES_PORT=5432

DATABASE_URL=postgresql+asyncpg://vpn3x:${POSTGRES_PASSWORD}@db:5432/vpn3x

# Redis
REDIS_URL=redis://redis:6379/0

# Application security
ENCRYPTION_KEY=${ENCRYPTION_KEY}
INTERNAL_API_KEY=${INTERNAL_API_KEY}

# Telegram bot
BOT_TOKEN=
BOT_ADMIN_IDS=

# Telegram notifications used by server
TELEGRAM_BOT_TOKEN=
TELEGRAM_ADMIN_IDS=

# Internal API
SERVER_API_URL=http://server:8000

# Remnawave -- the panel that owns every node. One panel for the whole
# deployment; nodes have no panel of their own.
# The token is minted in the panel under Settings -> API Tokens.
REMNAWAVE_BASE_URL=
REMNAWAVE_TOKEN=
REMNAWAVE_CADDY_TOKEN=
# The address nodes reach the panel on -- normally this server's public IP.
# Node bootstrap writes the node's firewall rule for NODE_PORT against this,
# so an empty value leaves that port closed and the node cannot connect.
REMNAWAVE_PANEL_ADDRESS=
REMNAWAVE_NODE_PORT=2222

# Telegram support
TELEGRAM_SUPPORT_CHAT_ID=

EOF

    chmod 600 "$ENV_FILE"

    echo ".env создан"

else

    echo ".env уже существует."
    echo "Существующие секреты будут сохранены."

fi


# ============================================================
# PostgreSQL
# ============================================================

log "Настройка PostgreSQL"

POSTGRES_USER="$(get_env POSTGRES_USER || true)"
POSTGRES_PASSWORD="$(get_env POSTGRES_PASSWORD || true)"
POSTGRES_DB="$(get_env POSTGRES_DB || true)"

if [[ -z "$POSTGRES_USER" ]]; then
    POSTGRES_USER="vpn3x"
    set_env POSTGRES_USER "$POSTGRES_USER"
fi

if [[ -z "$POSTGRES_DB" ]]; then
    POSTGRES_DB="vpn3x"
    set_env POSTGRES_DB "$POSTGRES_DB"
fi

if [[ -z "$POSTGRES_PASSWORD" ]]; then
    POSTGRES_PASSWORD="$(generate_secret)"
    set_env POSTGRES_PASSWORD "$POSTGRES_PASSWORD"
fi

set_env \
    DATABASE_URL \
    "postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB}"

set_env POSTGRES_HOST "db"
set_env POSTGRES_PORT "5432"


# ============================================================
# Redis
# ============================================================

log "Настройка Redis"

set_env REDIS_URL "redis://redis:6379/0"


# ============================================================
# Application secrets
# ============================================================

log "Настройка секретов приложения"

if [[ -z "$(get_env ENCRYPTION_KEY || true)" ]]; then
    set_env ENCRYPTION_KEY "$(generate_fernet_key)"
fi

if [[ -z "$(get_env INTERNAL_API_KEY || true)" ]]; then
    set_env INTERNAL_API_KEY "$(generate_secret)"
fi

set_env SERVER_API_URL "http://server:8000"


# ============================================================
# Remnawave
# ============================================================

log "Настройка Remnawave"

# Declared up front: several branches below leave it untouched, and `set -u`
# turns an unset variable into a crash at the summary rather than a warning.
REMNAWAVE_MISSING=""
RW_ENV_FILE="${APP_DIR}/.env.remnawave"

# The panel is brought up here by default. Set REMNAWAVE_EXTERNAL=1 (or point
# REMNAWAVE_BASE_URL at something that is not our own container) to use a
# panel you already run instead -- then this whole section is skipped and the
# only thing you owe us is a token.
RW_EXISTING_URL="$(get_env REMNAWAVE_BASE_URL || true)"
RW_EXISTING_TOKEN="$(get_env REMNAWAVE_TOKEN || true)"

if [[ "${REMNAWAVE_EXTERNAL:-0}" == "1" ]] \
   || { [[ -n "$RW_EXISTING_URL" ]] && [[ "$RW_EXISTING_URL" != *"//remnawave:"* ]]; }; then

    log "Используется внешняя панель Remnawave: ${RW_EXISTING_URL:-<не задана>}"
    RW_MANAGED=0

    if [[ -z "$RW_EXISTING_URL" || -z "$RW_EXISTING_TOKEN" ]]; then
        REMNAWAVE_MISSING="REMNAWAVE_BASE_URL / REMNAWAVE_TOKEN (внешняя панель)"
    fi

else
    RW_MANAGED=1
    COMPOSE_PROFILE_ARGS="--profile remnawave"
    set_env REMNAWAVE_BASE_URL "http://remnawave:3000"
fi

set_env REMNAWAVE_NODE_PORT "${REMNAWAVE_NODE_PORT:-2222}"

# Guessed, not demanded: it is only a default the admin can correct, and a
# wrong guess is visible in .env rather than silently baked into a node's
# firewall rule. Left empty if we cannot work it out.
if [[ -z "$(get_env REMNAWAVE_PANEL_ADDRESS || true)" ]]; then
    DETECTED_IP="$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || true)"
    if [[ -n "$DETECTED_IP" ]]; then
        set_env REMNAWAVE_PANEL_ADDRESS "$DETECTED_IP"
        echo "REMNAWAVE_PANEL_ADDRESS определён автоматически: $DETECTED_IP"
    else
        set_env REMNAWAVE_PANEL_ADDRESS ""
        echo "⚠️  Не удалось определить публичный IP — впишите"
        echo "    REMNAWAVE_PANEL_ADDRESS в .env вручную, иначе ноды не подключатся."
    fi
fi

# --- the panel's own env, separate from ours ------------------------------
#
# Written even when an external panel is used: docker-compose.yml references
# this file, and `docker compose --profile remnawave` refuses to run at all
# when an env_file is missing. An unused file costs nothing; a compose that
# fails on every boot does.
#
# Written once and then left alone. APP_SECRET is what the panel signs its
# JWTs with, so regenerating it on a re-run would invalidate every session
# and every API token we have already issued.
if [[ ! -f "$RW_ENV_FILE" ]]; then

    log "Создание .env.remnawave"

    RW_DB_PASSWORD="$(openssl rand -hex 24)"

    cat > "$RW_ENV_FILE" <<EOF
### Панель Remnawave. Отдельный файл, а не наш .env: у панели своя
### переменная TELEGRAM_BOT_TOKEN, и отдать ей наш .env значило бы молча
### передать ей токен нашего бота.
APP_PORT=3000
METRICS_PORT=3001
API_INSTANCES=1

POSTGRES_USER=remnawave
POSTGRES_PASSWORD=${RW_DB_PASSWORD}
POSTGRES_DB=remnawave
DATABASE_URL=postgresql://remnawave:${RW_DB_PASSWORD}@remnawave-db:5432/remnawave

REDIS_SOCKET=/var/run/valkey/valkey.sock

APP_SECRET=$(openssl rand -hex 64)
METRICS_USER=metrics
METRICS_PASS=$(openssl rand -hex 32)
WEBHOOK_SECRET_HEADER=$(openssl rand -hex 64)

IS_TELEGRAM_NOTIFICATIONS_ENABLED=false
WEBHOOK_ENABLED=false

FRONT_END_DOMAIN=*
SUB_PUBLIC_DOMAIN=${SUB_PUBLIC_DOMAIN:-127.0.0.1:3000/api/sub}
EOF

    chmod 600 "$RW_ENV_FILE"
    echo ".env.remnawave создан"

else
    echo ".env.remnawave уже существует — секреты панели сохранены."
fi

# ============================================================
# Telegram
# ============================================================

log "Настройка Telegram"

BOT_TOKEN="$(get_env BOT_TOKEN || true)"
BOT_ADMIN_IDS="$(get_env BOT_ADMIN_IDS || true)"

TELEGRAM_BOT_TOKEN="$(get_env TELEGRAM_BOT_TOKEN || true)"
TELEGRAM_ADMIN_IDS="$(get_env TELEGRAM_ADMIN_IDS || true)"


# ------------------------------------------------------------
# Bot token
# ------------------------------------------------------------

if [[ -z "$BOT_TOKEN" && -z "$TELEGRAM_BOT_TOKEN" ]]; then

    echo
    echo "Введите токен Telegram-бота."
    echo

    read -r -p "TELEGRAM BOT TOKEN: " BOT_TOKEN

    [[ -n "$BOT_TOKEN" ]] \
        || die "Токен Telegram-бота не может быть пустым."

    TELEGRAM_BOT_TOKEN="$BOT_TOKEN"

elif [[ -z "$BOT_TOKEN" ]]; then

    BOT_TOKEN="$TELEGRAM_BOT_TOKEN"

elif [[ -z "$TELEGRAM_BOT_TOKEN" ]]; then

    TELEGRAM_BOT_TOKEN="$BOT_TOKEN"

fi


# ------------------------------------------------------------
# Admin ID
# ------------------------------------------------------------

if [[ -z "$BOT_ADMIN_IDS" && -z "$TELEGRAM_ADMIN_IDS" ]]; then

    echo
    echo "Введите Telegram ID администратора."
    echo
    echo "Например:"
    echo "123456789"
    echo
    echo "Для нескольких администраторов:"
    echo "123456789,987654321"
    echo

    read -r -p "TELEGRAM ADMIN ID(S): " BOT_ADMIN_IDS

    [[ -n "$BOT_ADMIN_IDS" ]] \
        || die "Telegram admin ID не может быть пустым."

    TELEGRAM_ADMIN_IDS="$BOT_ADMIN_IDS"

elif [[ -z "$BOT_ADMIN_IDS" ]]; then

    BOT_ADMIN_IDS="$TELEGRAM_ADMIN_IDS"

elif [[ -z "$TELEGRAM_ADMIN_IDS" ]]; then

    TELEGRAM_ADMIN_IDS="$BOT_ADMIN_IDS"

fi


# ------------------------------------------------------------
# Write both configurations
# ------------------------------------------------------------

set_env BOT_TOKEN "$BOT_TOKEN"
set_env BOT_ADMIN_IDS "$BOT_ADMIN_IDS"

set_env TELEGRAM_BOT_TOKEN "$TELEGRAM_BOT_TOKEN"
set_env TELEGRAM_ADMIN_IDS "$TELEGRAM_ADMIN_IDS"

chmod 600 "$ENV_FILE"


# ============================================================
# Show Telegram configuration
# ============================================================

echo
echo "Telegram configuration:"
echo
echo "BOT_TOKEN:              configured"
echo "BOT_ADMIN_IDS:          ${BOT_ADMIN_IDS}"
echo "TELEGRAM_BOT_TOKEN:     configured"
echo "TELEGRAM_ADMIN_IDS:     ${TELEGRAM_ADMIN_IDS}"
echo
echo "Remnawave:"
echo
echo "REMNAWAVE_BASE_URL:      $(get_env REMNAWAVE_BASE_URL || true)"
echo "REMNAWAVE_TOKEN:         $([[ -n "$(get_env REMNAWAVE_TOKEN || true)" ]] && echo configured || echo "НЕ ЗАДАН")"
echo "REMNAWAVE_PANEL_ADDRESS: $(get_env REMNAWAVE_PANEL_ADDRESS || true)"
echo


# ============================================================
# Validate compose
# ============================================================

log "Проверка Docker Compose"

docker compose \
    --env-file "$ENV_FILE" \
    -f "$COMPOSE_FILE" \
    $COMPOSE_PROFILE_ARGS \
    config >/dev/null

echo "docker-compose.yml корректен"


# ============================================================
# Stop previous installation
# ============================================================

log "Остановка существующего VPN-3X"

docker compose \
    --env-file "$ENV_FILE" \
    -f "$COMPOSE_FILE" \
    down --remove-orphans || true


# ============================================================
# Pull infrastructure
# ============================================================

log "Загрузка PostgreSQL и Redis"

docker compose \
    --env-file "$ENV_FILE" \
    -f "$COMPOSE_FILE" \
    pull db redis


# ============================================================
# Build application
# ============================================================

log "Сборка server / worker / bot"

docker compose \
    --env-file "$ENV_FILE" \
    -f "$COMPOSE_FILE" \
    build --pull server worker bot


# ============================================================
# Start PostgreSQL + Redis
# ============================================================

log "Запуск PostgreSQL и Redis"

docker compose \
    --env-file "$ENV_FILE" \
    -f "$COMPOSE_FILE" \
    up -d db redis


# ============================================================
# Wait for PostgreSQL
# ============================================================

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
        logs --tail=150 db

    die "PostgreSQL не запустился."

fi

echo "PostgreSQL готов"


# ============================================================
# Start server
# ============================================================

log "Запуск server"

docker compose \
    --env-file "$ENV_FILE" \
    -f "$COMPOSE_FILE" \
    up -d server

sleep 5


# ============================================================
# Check server container
# ============================================================

SERVER_CONTAINER="$(
    docker compose \
        --env-file "$ENV_FILE" \
        -f "$COMPOSE_FILE" \
        ps -q server
)"

if [[ -z "$SERVER_CONTAINER" ]]; then

    docker compose \
        --env-file "$ENV_FILE" \
        -f "$COMPOSE_FILE" \
        logs --tail=150 server

    die "Контейнер server не создан."

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

    die "Контейнер server не запущен."

fi


# ============================================================
# Start the Remnawave panel and mint its API token
# ============================================================

if [[ "$RW_MANAGED" == "1" ]]; then

    log "Запуск панели Remnawave"

    docker compose \
        --env-file "$ENV_FILE" \
        -f "$COMPOSE_FILE" \
        $COMPOSE_PROFILE_ARGS \
        up -d remnawave-db remnawave-redis remnawave

    # Credentials are generated once and kept in .env, because they are the
    # only way back into the panel: registration closes after the first
    # account exists, so losing them means the admin UI is unreachable even
    # though our API token keeps working.
    RW_ADMIN_USER="$(get_env REMNAWAVE_ADMIN_USER || true)"
    RW_ADMIN_PASS="$(get_env REMNAWAVE_ADMIN_PASSWORD || true)"

    if [[ -z "$RW_ADMIN_USER" ]]; then
        RW_ADMIN_USER="vpn3xadmin"
        set_env REMNAWAVE_ADMIN_USER "$RW_ADMIN_USER"
    fi

    if [[ -z "$RW_ADMIN_PASS" ]]; then
        # The panel requires >=24 chars with upper, lower and a digit.
        RW_ADMIN_PASS="Vpn3x$(openssl rand -hex 16)A1"
        set_env REMNAWAVE_ADMIN_PASSWORD "$RW_ADMIN_PASS"
    fi

    if [[ -n "$(get_env REMNAWAVE_TOKEN || true)" ]]; then

        echo "REMNAWAVE_TOKEN уже задан — пропускаю выпуск нового."

    else

        log "Выпуск API-токена Remnawave"

        # Run inside the server container: it is on the compose network, so
        # http://remnawave:3000 resolves, and it already has httpx.
        set +e
        RW_TOKEN="$(
            docker compose \
                --env-file "$ENV_FILE" \
                -f "$COMPOSE_FILE" \
                run --rm --no-deps \
                -v "${APP_DIR}/scripts:/scripts:ro" \
                server \
                python3 /scripts/provision_panel.py \
                    "http://remnawave:3000" \
                    "$RW_ADMIN_USER" \
                    "$RW_ADMIN_PASS" \
                    "vpn-3x"
        )"
        RW_TOKEN_STATUS=$?
        set -e

        # Belt and braces: `docker compose run` can prepend its own noise to
        # stdout, and an empty value would be written to .env as if it had
        # worked.
        RW_TOKEN="$(printf '%s' "$RW_TOKEN" | tr -d '\r' | tail -n 1 | tr -d '[:space:]')"

        if [[ $RW_TOKEN_STATUS -ne 0 || -z "$RW_TOKEN" ]]; then
            echo
            echo "⚠️  Не удалось выпустить токен автоматически."
            echo "    Панель работает; создайте токен вручную и впишите в .env:"
            echo "      ssh -L 3000:127.0.0.1:3000 root@<этот сервер>"
            echo "      http://127.0.0.1:3000  ->  Settings -> API Tokens"
            echo "      логин: $RW_ADMIN_USER"
            echo "      пароль: $RW_ADMIN_PASS"
            echo
            REMNAWAVE_MISSING="REMNAWAVE_TOKEN"
        else
            set_env REMNAWAVE_TOKEN "$RW_TOKEN"
            echo "API-токен Remnawave выпущен и записан в .env"
            REMNAWAVE_MISSING=""
        fi

    fi

fi

# ============================================================
# Database migrations
# ============================================================

log "Запуск миграций Alembic"

docker compose \
    --env-file "$ENV_FILE" \
    -f "$COMPOSE_FILE" \
    run \
    --rm \
    --no-deps \
    -v "${APP_DIR}:/workspace:ro" \
    server \
    sh -c 'cd /workspace/migrations && alembic -c alembic.ini upgrade head'

echo "Миграции выполнены"


# ============================================================
# Start worker + bot
# ============================================================

log "Запуск worker и bot"

docker compose \
    --env-file "$ENV_FILE" \
    -f "$COMPOSE_FILE" \
    up -d worker bot


# ============================================================
# Wait for API
# ============================================================

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
    echo "SERVER LOGS:"
    echo

    docker compose \
        --env-file "$ENV_FILE" \
        -f "$COMPOSE_FILE" \
        logs --tail=150 server

    die "API health check не пройден."

fi

echo "API работает"


# ============================================================
# Check worker
# ============================================================

log "Проверка worker"

WORKER_CONTAINER="$(
    docker compose \
        --env-file "$ENV_FILE" \
        -f "$COMPOSE_FILE" \
        ps -q worker
)"

if [[ -z "$WORKER_CONTAINER" ]]; then
    die "Worker контейнер не создан."
fi

WORKER_STATUS="$(
    docker inspect \
        --format '{{.State.Status}}' \
        "$WORKER_CONTAINER"
)"

if [[ "$WORKER_STATUS" != "running" ]]; then

    docker compose \
        --env-file "$ENV_FILE" \
        -f "$COMPOSE_FILE" \
        logs --tail=150 worker

    die "Worker не запущен."

fi


# ============================================================
# Check bot
# ============================================================

log "Проверка Telegram bot"

BOT_CONTAINER="$(
    docker compose \
        --env-file "$ENV_FILE" \
        -f "$COMPOSE_FILE" \
        ps -q bot
)"

if [[ -z "$BOT_CONTAINER" ]]; then
    die "Bot контейнер не создан."
fi

BOT_STATUS="$(
    docker inspect \
        --format '{{.State.Status}}' \
        "$BOT_CONTAINER"
)"

if [[ "$BOT_STATUS" != "running" ]]; then

    docker compose \
        --env-file "$ENV_FILE" \
        -f "$COMPOSE_FILE" \
        logs --tail=150 bot

    die "Bot не запущен."

fi


# ============================================================
# Verify admin configuration inside container
# ============================================================

log "Проверка конфигурации администратора"

BOT_ADMIN_VALUE="$(
    docker compose \
        --env-file "$ENV_FILE" \
        -f "$COMPOSE_FILE" \
        exec -T bot \
        sh -c 'printf "%s" "$BOT_ADMIN_IDS"'
)"

if [[ -z "$BOT_ADMIN_VALUE" ]]; then
    die "BOT_ADMIN_IDS не попал внутрь bot-контейнера."
fi

echo "BOT_ADMIN_IDS внутри контейнера: $BOT_ADMIN_VALUE"


# ============================================================
# Systemd
# ============================================================

log "Настройка автозапуска"

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

ExecStart=/usr/bin/docker compose --env-file ${ENV_FILE} -f ${COMPOSE_FILE} ${COMPOSE_PROFILE_ARGS} up -d
ExecStop=/usr/bin/docker compose --env-file ${ENV_FILE} -f ${COMPOSE_FILE} ${COMPOSE_PROFILE_ARGS} down

TimeoutStartSec=0
TimeoutStopSec=120

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable vpn-3x.service


# ============================================================
# Final status
# ============================================================

log "Финальная проверка"

# The server, worker and bot were started before the token existed, so they
# are holding a config without it. Recreate them now rather than leaving the
# admin to discover it at the end of the add-node wizard.
if [[ "$RW_MANAGED" == "1" && -n "$(get_env REMNAWAVE_TOKEN || true)" ]]; then
    log "Перезапуск сервисов с токеном панели"
    docker compose \
        --env-file "$ENV_FILE" \
        -f "$COMPOSE_FILE" \
        $COMPOSE_PROFILE_ARGS \
        up -d --force-recreate server worker bot
    sleep 8
fi

docker compose \
    --env-file "$ENV_FILE" \
    -f "$COMPOSE_FILE" \
    $COMPOSE_PROFILE_ARGS \
    ps

echo
echo "API:"
curl -fsS http://127.0.0.1:8000/health
echo
echo "Зависимости:"
curl -fsS -H "X-API-Key: $(get_env INTERNAL_API_KEY)" \
    http://127.0.0.1:8000/health/ready || true

echo
echo
echo "============================================================"
echo "VPN-3X УСПЕШНО УСТАНОВЛЕН"
echo "============================================================"
echo
echo "Проект:"
echo "  $APP_DIR"
echo
echo "Конфигурация:"
echo "  $ENV_FILE"
echo
echo "Telegram admin IDs:"
echo "  $BOT_ADMIN_IDS"
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
echo "  docker compose logs -f bot"
echo "  docker compose logs -f server"
echo "  docker compose logs -f worker"
echo
echo "  systemctl status vpn-3x.service"
echo "  systemctl restart vpn-3x.service"
echo

# Printed once, here, because registration closes after the first account:
# these credentials are the only way into the panel's UI, and they are not
# recoverable from it. They stay in .env (chmod 600) as well.
if [[ "$RW_MANAGED" == "1" ]]; then
    echo "Панель Remnawave:"
    echo
    echo "  Доступ только с этого сервера (наружу не опубликована)."
    echo "  С вашей машины:  ssh -L 3000:127.0.0.1:3000 ${SUDO_USER:-root}@$(get_env REMNAWAVE_PANEL_ADDRESS || echo '<ip>')"
    echo "  Затем откройте:  http://127.0.0.1:3000"
    echo
    echo "  Логин:  $(get_env REMNAWAVE_ADMIN_USER || true)"
    echo "  Пароль: $(get_env REMNAWAVE_ADMIN_PASSWORD || true)"
    echo
    echo "  Эти данные больше нигде не восстановить: регистрация в панели"
    echo "  закрывается после создания первого администратора."
    echo
fi

# Said last, and loudly: without these, adding a node fails at the very end
# of the wizard, after the admin has already typed everything in.
if [[ -n "$REMNAWAVE_MISSING" ]]; then
    echo "============================================================"
    echo
    echo "  ⚠️  НЕ ЗАДАНО: $REMNAWAVE_MISSING"
    echo
    echo "  Без этого установка нод работать не будет: главный сервер"
    echo "  управляет нодами только через панель Remnawave."
    echo
    echo "  1) Поднимите панель (здесь же, рядом с сервером):"
    echo "       cd $APP_DIR && docker compose --profile remnawave up -d"
    echo "     либо укажите адрес панели, которая у вас уже есть."
    echo "  2) В панели: Settings -> API Tokens -> создайте токен."
    echo "  3) Впишите в $ENV_FILE:"
    echo "       REMNAWAVE_BASE_URL=http://remnawave:3000"
    echo "       REMNAWAVE_TOKEN=<токен из панели>"
    echo "  4) docker compose up -d"
    echo
fi

echo "============================================================"