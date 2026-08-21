#!/usr/bin/env bash
set -euo pipefail

# Installs the VPN-3X main server (FastAPI server + arq worker + Telegram
# bot + PostgreSQL + Redis, all via docker-compose.yml) onto a fresh
# server. Run this FROM a clone of this repo, as root, on the machine that
# will host the *main* server -- VPN nodes are a separate thing, added
# later from inside the bot's admin panel (see PLAN.md Etap 1).
#
# Targets a bare Ubuntu/Debian box: everything it needs that isn't already
# there (curl, python3, python3-venv, Docker, the compose plugin) is
# installed below rather than assumed.
#
# Usage: sudo ./install.sh
#
# Safe to re-run: an existing .env is left untouched, and every step below
# (apt installs, docker install, migrations, compose up) is a no-op or an
# idempotent update if it already ran once.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

log()  { echo -e "\n>>> $*"; }
warn() { echo "WARNING: $*" >&2; }
die()  { echo "ERROR: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root (sudo ./install.sh)"
[[ -f docker-compose.yml ]] || die "run this from inside a clone of the VPN-3X repo"

export DEBIAN_FRONTEND=noninteractive

# ---------------------------------------------------------------------------
# 1. Base OS packages
#
# A freshly provisioned Ubuntu image may have none of these. python3-venv in
# particular is split out of python3 on Debian/Ubuntu, so `python3 -m venv`
# fails with "ensurepip is not available" without it -- which is exactly
# what breaks the migration step further down.
# ---------------------------------------------------------------------------
APT_UPDATED=0
apt_update_once() {
  if [[ $APT_UPDATED -eq 0 ]]; then
    log "Updating apt package lists..."
    # Explicit message: bare `set -e` on a failing apt would abort with just
    # an exit code, which says nothing about what to fix.
    apt-get update -qq || die "apt-get update failed -- check this machine's network/DNS and /etc/apt/sources.list"
    APT_UPDATED=1
  fi
}

apt_install() {
  apt_update_once
  apt-get install -y -qq "$@" >/dev/null || die "failed to install: $* -- see the apt output above"
}

log "Checking base packages (curl, ca-certificates, python3)..."
MISSING_BASE=()
command -v curl >/dev/null      || MISSING_BASE+=(curl)
command -v python3 >/dev/null   || MISSING_BASE+=(python3)
[[ -e /etc/ssl/certs/ca-certificates.crt ]] || MISSING_BASE+=(ca-certificates)
if [[ ${#MISSING_BASE[@]} -gt 0 ]]; then
  log "Installing: ${MISSING_BASE[*]}"
  apt_install "${MISSING_BASE[@]}"
fi
command -v python3 >/dev/null || die "python3 still missing after apt install -- unsupported OS?"

# `python3 -m venv` needs ensurepip, which Debian/Ubuntu ship in a separate
# python3-venv package. The generic name pulls the right one for the default
# interpreter, but on images where python3 is a newer/alt version only the
# versioned package exists -- try both before giving up.
ensure_venv_support() {
  if python3 -c 'import ensurepip' 2>/dev/null; then
    return 0
  fi
  local py_ver
  py_ver="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  log "python3 -m venv is unavailable (no ensurepip) -- installing venv support for Python ${py_ver}..."
  apt_update_once
  apt-get install -y -qq "python${py_ver}-venv" >/dev/null 2>&1 \
    || apt-get install -y -qq python3-venv >/dev/null 2>&1 \
    || true
  python3 -c 'import ensurepip' 2>/dev/null \
    || die "couldn't enable venv support -- install python${py_ver}-venv (or python3-venv) manually and re-run"
}
ensure_venv_support

# ---------------------------------------------------------------------------
# 2. Docker
# ---------------------------------------------------------------------------
if ! command -v docker >/dev/null; then
  log "Docker not found -- installing via the official get.docker.com script..."
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker >/dev/null 2>&1 || true

docker compose version >/dev/null 2>&1 \
  || die "docker compose plugin missing even after installing Docker -- check your OS is supported by get.docker.com"
docker info >/dev/null 2>&1 \
  || die "the Docker daemon isn't reachable -- try: systemctl start docker"

# ---------------------------------------------------------------------------
# 3. .env -- generate real secrets, only ask the admin for what we can't
#    generate ourselves (the Telegram bot token/admin IDs). Everything else
#    tunable (subscription price, ad durations, the CryptoBot payment
#    token, Cloudflare...) is configured LATER from the bot's admin panel --
#    see the printed next-steps at the end, not here.
# ---------------------------------------------------------------------------
if [[ -f .env ]]; then
  log ".env already exists -- leaving it as-is. Delete it first if you want to regenerate secrets."
else
  log "Generating .env..."
  cp .env.example .env

  gen_secret() { python3 -c "import secrets; print(secrets.token_urlsafe(32))"; }

  POSTGRES_PASSWORD="$(gen_secret)"
  INTERNAL_API_KEY="$(gen_secret)"

  # A Fernet key is just 32 random bytes, base64url-encoded -- generate it
  # from the stdlib rather than requiring `cryptography` on the host Python
  # (it isn't installed at this point, and the app that consumes this key
  # runs in a container that has it).
  ENCRYPTION_KEY="$(python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())")"

  BOT_TOKEN="${BOT_TOKEN:-}"
  while [[ -z "$BOT_TOKEN" ]]; do
    read -rp "Telegram bot token (from @BotFather): " BOT_TOKEN
  done
  BOT_ADMIN_IDS="${BOT_ADMIN_IDS:-}"
  while [[ -z "$BOT_ADMIN_IDS" ]]; do
    read -rp "Your Telegram numeric user ID(s), comma-separated, allowed to use the admin panel: " BOT_ADMIN_IDS
  done

  sed -i \
    -e "s#^POSTGRES_PASSWORD=.*#POSTGRES_PASSWORD=${POSTGRES_PASSWORD}#" \
    -e "s#^DATABASE_URL=.*#DATABASE_URL=postgresql+asyncpg://vpn:${POSTGRES_PASSWORD}@db:5432/vpn3x#" \
    -e "s#^ENCRYPTION_KEY=.*#ENCRYPTION_KEY=${ENCRYPTION_KEY}#" \
    -e "s#^INTERNAL_API_KEY=.*#INTERNAL_API_KEY=${INTERNAL_API_KEY}#" \
    -e "s#^BOT_TOKEN=.*#BOT_TOKEN=${BOT_TOKEN}#" \
    -e "s#^BOT_ADMIN_IDS=.*#BOT_ADMIN_IDS=${BOT_ADMIN_IDS}#" \
    -e "s#^TELEGRAM_BOT_TOKEN=.*#TELEGRAM_BOT_TOKEN=${BOT_TOKEN}#" \
    -e "s#^TELEGRAM_ADMIN_IDS=.*#TELEGRAM_ADMIN_IDS=${BOT_ADMIN_IDS}#" \
    .env
  chmod 600 .env
  log ".env written (chmod 600, contains secrets)."
fi

# ---------------------------------------------------------------------------
# 4. Bring up PostgreSQL + Redis first, then run migrations against them
#    from the host -- migrations/env.py locates ../server relative to
#    itself on disk, which only resolves correctly outside the server
#    container's image, so this runs here rather than via `docker compose
#    run server ...`.
# ---------------------------------------------------------------------------
log "Starting PostgreSQL and Redis..."
docker compose up -d db redis

log "Waiting for PostgreSQL to report healthy..."
db_healthy=0
for _ in $(seq 1 60); do
  cid="$(docker compose ps -q db 2>/dev/null || true)"
  if [[ -n "$cid" ]]; then
    # Read the health state straight off the container rather than parsing
    # `docker compose ps` output, whose column formatting varies by version.
    state="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid" 2>/dev/null || echo none)"
    if [[ "$state" == "healthy" ]]; then
      db_healthy=1
      break
    fi
  fi
  sleep 2
done
[[ $db_healthy -eq 1 ]] || die "PostgreSQL never became healthy -- check: docker compose logs db"

log "Running database migrations..."
if [[ ! -d .venv-migrate ]]; then
  python3 -m venv .venv-migrate
fi
# Outside the `if` so a half-finished venv from an interrupted earlier run
# still gets its dependencies; pip skips whatever is already satisfied.
.venv-migrate/bin/pip install -q --upgrade pip >/dev/null
.venv-migrate/bin/pip install -q -r server/requirements.txt

set -a
# shellcheck disable=SC1091  # generated above / by a previous run
source .env
set +a
# Migrations run from the host, so they need Postgres's published port on
# localhost, not the `db` hostname that only resolves inside the compose
# network.
export DATABASE_URL="${DATABASE_URL/@db:/@localhost:}"
.venv-migrate/bin/alembic -c migrations/alembic.ini upgrade head

# ---------------------------------------------------------------------------
# 5. Build and start the server, worker, and bot
# ---------------------------------------------------------------------------
log "Building and starting the server, worker, and bot..."
docker compose up -d --build

log "Waiting for the API to answer..."
api_up=0
for _ in $(seq 1 30); do
  if curl -fsS http://localhost:8000/health >/dev/null 2>&1; then
    api_up=1
    break
  fi
  sleep 2
done
if [[ $api_up -eq 1 ]]; then
  log "API is up."
else
  warn "the API didn't answer on http://localhost:8000/health within ~60s."
  warn "The stack may still be starting; check with: docker compose logs server"
fi

echo
docker compose ps

cat <<'EOF'

============================================================
Install complete.

Next steps -- all from inside the bot in Telegram, as one of
the admin IDs you set. Everything is buttons, no commands to
memorise:

  1. Open your bot and send /start.
  2. Tap "Админ-панель".
  3. "Ноды" -> "Добавить ноду" -- it will ask for the IP and
     root password step by step, then install 3x-ui and set
     up REALITY on that server by itself.
  4. "Настройки" -> "Токен CryptoBot" to turn on payments
     (get the token from @CryptoBot -> /pay -> Create App).
  5. "Cloudflare" to put this server behind Cloudflare once
     you have a domain pointed at it.

Useful:
  docker compose ps          # service status
  docker compose logs -f bot # follow the bot's log
  docker compose restart     # restart everything
============================================================
EOF
