#!/usr/bin/env bash
set -euo pipefail

# Installs the VPN-3X main server (FastAPI server + arq worker + Telegram
# bot + PostgreSQL + Redis, all via docker-compose.yml) onto a fresh
# server. Run this FROM a clone of this repo, as root, on the machine that
# will host the *main* server -- VPN nodes are a separate thing, added
# later from inside the bot with /bootstrap (see PLAN.md Etap 1).
#
# Usage: sudo ./install.sh
#
# Safe to re-run: an existing .env is left untouched, and every step below
# (docker install, migrations, compose up) is a no-op or idempotent update
# if it already ran once.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

log()  { echo -e "\n>>> $*"; }
die()  { echo "ERROR: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root (sudo ./install.sh)"
[[ -f docker-compose.yml ]] || die "run this from inside a clone of the VPN-3X repo"
command -v python3 >/dev/null || die "python3 is required but not found"

# ---------------------------------------------------------------------------
# 1. Docker
# ---------------------------------------------------------------------------
if ! command -v docker >/dev/null; then
  log "Docker not found -- installing via the official get.docker.com script..."
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker >/dev/null 2>&1 || true

docker compose version >/dev/null 2>&1 || die "docker compose plugin missing even after installing Docker -- check your OS is supported by get.docker.com"

# ---------------------------------------------------------------------------
# 2. .env -- generate real secrets, only ask the admin for what we can't
#    generate ourselves (the Telegram bot token/admin IDs). Everything else
#    tunable (subscription price, ad durations, the CryptoBot payment
#    token...) is configured LATER from inside the running bot -- see the
#    printed next-steps at the end, not here.
# ---------------------------------------------------------------------------
if [[ -f .env ]]; then
  log ".env already exists -- leaving it as-is. Delete it first if you want to regenerate secrets."
else
  log "Generating .env..."
  cp .env.example .env

  gen_secret() { python3 -c "import secrets; print(secrets.token_urlsafe(32))"; }
  gen_fernet_key() {
    python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" 2>/dev/null
  }

  POSTGRES_PASSWORD="$(gen_secret)"
  INTERNAL_API_KEY="$(gen_secret)"

  ENCRYPTION_KEY="$(gen_fernet_key || true)"
  if [[ -z "$ENCRYPTION_KEY" ]]; then
    # `cryptography` isn't guaranteed on a bare host's system Python --
    # fall back to a throwaway venv just to generate this one key.
    python3 -m venv /tmp/vpn3x-keygen-venv
    /tmp/vpn3x-keygen-venv/bin/pip install -q cryptography
    ENCRYPTION_KEY="$(/tmp/vpn3x-keygen-venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")"
    rm -rf /tmp/vpn3x-keygen-venv
  fi

  BOT_TOKEN="${BOT_TOKEN:-}"
  if [[ -z "$BOT_TOKEN" ]]; then
    read -rp "Telegram bot token (from @BotFather): " BOT_TOKEN
  fi
  BOT_ADMIN_IDS="${BOT_ADMIN_IDS:-}"
  if [[ -z "$BOT_ADMIN_IDS" ]]; then
    read -rp "Your Telegram numeric user ID(s), comma-separated, allowed to use the admin menu: " BOT_ADMIN_IDS
  fi

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
# 3. Bring up PostgreSQL + Redis first, then run migrations against them
#    from the host -- migrations/env.py locates ../server relative to
#    itself on disk, which only resolves correctly outside the server
#    container's image, so this runs here rather than via `docker compose
#    run server ...`.
# ---------------------------------------------------------------------------
log "Starting PostgreSQL and Redis..."
docker compose up -d db redis

log "Waiting for PostgreSQL to report healthy..."
for i in $(seq 1 30); do
  if docker compose ps db 2>/dev/null | grep -q "healthy"; then
    break
  fi
  [[ $i -eq 30 ]] && die "PostgreSQL never became healthy -- check: docker compose logs db"
  sleep 2
done

log "Running database migrations..."
if [[ ! -d .venv-migrate ]]; then
  python3 -m venv .venv-migrate
  .venv-migrate/bin/pip install -q -r server/requirements.txt
fi
set -a
source .env
set +a
# Migrations run from the host, so they need Postgres's published port on
# localhost, not the `db` hostname that only resolves inside the compose
# network.
export DATABASE_URL="${DATABASE_URL/@db:/@localhost:}"
.venv-migrate/bin/alembic -c migrations/alembic.ini upgrade head

# ---------------------------------------------------------------------------
# 4. Build and start the server, worker, and bot
# ---------------------------------------------------------------------------
log "Building and starting the server, worker, and bot..."
docker compose up -d --build

log "Install complete. Check status with: docker compose ps"
cat <<'EOF'

Next steps (all done from inside the bot in Telegram, as one of the admin
IDs you just set):
  1. Open the bot and confirm /start responds.
  2. Add your first VPN node (installs 3x-ui over SSH, sets up REALITY):
       /bootstrap <name> <ip> <ssh_root_password> [country]
  3. Turn on crypto payments (token from @CryptoBot -> /pay -> Create App):
       /setcryptobottoken <token>
  4. Review/tune price, ad durations, alert threshold:
       /settings
  5. To put the main server behind Cloudflare once you have a domain:
       ./scripts/setup_cloudflare.sh <api_token> <zone_id> <record_name> <server_ip>
EOF
