#!/usr/bin/env bash
# Runs the node doctor from the main server and prints the report.
#
# Exists because the doctor is otherwise only reachable through the bot, and
# the one moment you need it most -- a VPN that connects and carries no
# traffic -- is the moment Telegram will not load, because the broken tunnel
# is capturing your traffic and dropping it. This needs no Telegram, no
# phone, and no working VPN: run it over SSH on the main server.
#
# Usage, from the repo root on the main server:
#   ./scripts/diagnose.sh              # every node
#   ./scripts/diagnose.sh <node-id>    # one node, plus its xray log
#
# Reads INTERNAL_API_KEY from .env (or the environment). Talks to the server
# container over the compose network, so it works whether or not Cloudflare,
# DNS or the bot are set up -- none of those are in this path.

set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  # Only the key we need, so a stray line in .env cannot clobber the shell.
  INTERNAL_API_KEY="${INTERNAL_API_KEY:-$(grep -E '^INTERNAL_API_KEY=' .env | tail -1 | cut -d= -f2-)}"
fi

: "${INTERNAL_API_KEY:?INTERNAL_API_KEY not found in .env or the environment}"

NODE_ID="${1:-}"

# Runs inside the server container, via python3 rather than curl: the image
# is built from a slim Python base and does not ship curl. Nothing has to be
# installed on the host, and the API port does not have to be published.
api() {
  docker compose exec -T -e VPN3X_KEY="$INTERNAL_API_KEY" -e VPN3X_PATH="$1" server \
    python3 -c '
import json, os, sys, urllib.error, urllib.request

request = urllib.request.Request(
    "http://127.0.0.1:8000" + os.environ["VPN3X_PATH"],
    headers={"X-API-Key": os.environ["VPN3X_KEY"]},
)
try:
    with urllib.request.urlopen(request, timeout=60) as response:
        body = response.read().decode()
except urllib.error.HTTPError as exc:
    # The API reports real problems as 4xx/5xx with a JSON detail -- print
    # that rather than a stack trace, it is usually the answer.
    print(f"HTTP {exc.code}: {exc.read().decode()[:2000]}", file=sys.stderr)
    sys.exit(1)
except Exception as exc:
    print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
    sys.exit(1)

try:
    print(json.dumps(json.loads(body), indent=2, ensure_ascii=False))
except ValueError:
    print(body)
'
}

if [[ -z "$NODE_ID" ]]; then
  echo "== Ноды =="
  api "/nodes"
  echo
  echo "Запустите ./scripts/diagnose.sh <node-id>, чтобы разобрать конкретную ноду."
  exit 0
fi

echo "== Диагностика ноды $NODE_ID =="
api "/nodes/$NODE_ID/diagnose"

echo
echo "== Состояние ноды в панели =="
# Remnawave's API does not expose the node's xray log -- it lives in the
# node's own container. The endpoint returns the panel's view and the exact
# command for the log itself.
api "/nodes/$NODE_ID/xray-log"
