#!/usr/bin/env bash
set -euo pipefail

# Puts the VPN-3X main server behind Cloudflare's CDN/proxy: creates or
# updates a proxied DNS record pointing at it, and sets a sane SSL mode.
# Re-run this whenever the server's IP changes -- it's idempotent (updates
# the existing record instead of duplicating it).
#
# Requires a domain already added to a Cloudflare account -- this only
# points a record at your server, it does not create the zone itself.
#
# Usage:
#   ./scripts/setup_cloudflare.sh <api_token> <zone_id> <record_name> <server_ip> [--lock-firewall]
#
#   api_token    A Cloudflare API TOKEN (not the legacy Global API Key),
#                scoped to Zone.DNS:Edit + Zone.Zone Settings:Edit for this
#                zone. Create one at:
#                https://dash.cloudflare.com/profile/api-tokens
#   zone_id      From the domain's Overview page in the Cloudflare dashboard.
#   record_name  e.g. vpn.example.com or example.com
#   server_ip    The main server's public IPv4 address.
#   --lock-firewall
#                Optional. Additionally ALLOWS inbound 80/443 from
#                Cloudflare's published IP ranges via ufw. Does not touch
#                SSH or remove any pre-existing rule that already allows
#                80/443 from anywhere -- see the printed notice at the end
#                for that; this script only adds rules, it never deletes
#                one, since guessing which existing firewall rule is safe
#                to remove on a box we can't otherwise reach is not
#                something to automate.

die() { echo "ERROR: $*" >&2; exit 1; }

[[ $# -ge 4 ]] || die "Usage: $0 <api_token> <zone_id> <record_name> <server_ip> [--lock-firewall]"
API_TOKEN="$1"; ZONE_ID="$2"; RECORD_NAME="$3"; SERVER_IP="$4"; LOCK_FIREWALL="${5:-}"

if ! command -v jq >/dev/null; then
  echo "Installing jq..."
  apt-get update -y && apt-get install -y jq
fi

API="https://api.cloudflare.com/client/v4"
AUTH_HEADER="Authorization: Bearer ${API_TOKEN}"

# No `-f`: Cloudflare returns a JSON body with `success`/`errors` even on
# 4xx responses (e.g. a bad token), and that body is more useful than
# curl's bare exit code -- `-f` would discard it and abort before we get to
# read it. `-S` still surfaces a real transport-level failure (DNS, refused,
# timeout), which curl does treat as a non-zero exit either way.
cf_get()   { curl -sS -H "$AUTH_HEADER" "$@"; }
cf_write() { curl -sS -H "$AUTH_HEADER" -H "Content-Type: application/json" "$@"; }

echo ">>> Verifying API token and zone..."
zone_check="$(cf_get "${API}/zones/${ZONE_ID}")"
if [[ "$(echo "$zone_check" | jq -r .success)" != "true" ]]; then
  die "Cloudflare API token/zone check failed: $(echo "$zone_check" | jq -c .errors)"
fi
zone_name="$(echo "$zone_check" | jq -r .result.name)"
echo "Zone OK: ${zone_name}"

echo ">>> Looking for an existing A record for ${RECORD_NAME}..."
existing="$(cf_get "${API}/zones/${ZONE_ID}/dns_records?type=A&name=${RECORD_NAME}")"
record_id="$(echo "$existing" | jq -r '.result[0].id // empty')"

body="$(jq -n --arg name "$RECORD_NAME" --arg ip "$SERVER_IP" \
  '{type: "A", name: $name, content: $ip, ttl: 1, proxied: true}')"

if [[ -n "$record_id" ]]; then
  echo ">>> Updating existing record (${record_id}) -> ${SERVER_IP}, proxied"
  result="$(cf_write -X PUT -d "$body" "${API}/zones/${ZONE_ID}/dns_records/${record_id}")"
else
  echo ">>> Creating new record ${RECORD_NAME} -> ${SERVER_IP}, proxied"
  result="$(cf_write -X POST -d "$body" "${API}/zones/${ZONE_ID}/dns_records")"
fi
[[ "$(echo "$result" | jq -r .success)" == "true" ]] || die "DNS record write failed: $(echo "$result" | jq -c .errors)"

echo ">>> Setting SSL/TLS mode to 'full' (encrypts Cloudflare<->origin traffic;"
echo "    upgrade to 'strict' later once the origin has a trusted/Cloudflare-issued cert)"
ssl_result="$(cf_write -X PATCH -d '{"value":"full"}' "${API}/zones/${ZONE_ID}/settings/ssl")"
[[ "$(echo "$ssl_result" | jq -r .success)" == "true" ]] || die "Setting SSL mode failed: $(echo "$ssl_result" | jq -c .errors)"

echo
echo "Done. ${RECORD_NAME} now resolves through Cloudflare to ${SERVER_IP}."
echo "DNS can take a few minutes to propagate -- check with: dig +short ${RECORD_NAME}"

if [[ "$LOCK_FIREWALL" == "--lock-firewall" ]]; then
  echo
  echo "WARNING: about to ALLOW inbound 80/443 from Cloudflare's current IP"
  echo "ranges via ufw. This does NOT touch SSH, and does NOT remove any rule"
  echo "you may already have that allows 80/443 from anywhere -- if one"
  echo "exists, Cloudflare is not actually enforced as the only path in until"
  echo "you remove that rule yourself (shown below to review, not deleted"
  echo "automatically)."
  read -rp "Continue? [y/N] " confirm
  if [[ "$confirm" == "y" || "$confirm" == "Y" ]]; then
    command -v ufw >/dev/null || (apt-get update -y && apt-get install -y ufw)
    for range in $(curl -fsS https://www.cloudflare.com/ips-v4) $(curl -fsS https://www.cloudflare.com/ips-v6); do
      ufw allow from "$range" to any port 80 proto tcp comment "cf-origin-lock" >/dev/null
      ufw allow from "$range" to any port 443 proto tcp comment "cf-origin-lock" >/dev/null
    done
    ufw --force enable
    echo "Added. Current rules for 80/443 -- review for any pre-existing"
    echo "'Anywhere' allow that would bypass this:"
    ufw status numbered | grep -E "80|443" || true
  else
    echo "Skipped firewall changes."
  fi
fi
