"""Builds the JSON payload 3x-ui expects to create/update a VLESS+REALITY+
XTLS inbound on tcp/443 (README: nodes only run vless+reality+xtls on tcp/443
or grpc on a free port -- this targets the tcp/443 case).

IMPORTANT: 3x-ui's inbound API takes `settings`, `streamSettings` and
`sniffing` as JSON-encoded STRINGS (its DB column is TEXT), not nested
objects -- that's a real quirk of the x-ui/3x-ui API, not a guess. The exact
field names inside realitySettings are the standard xray-core REALITY config
shape. Still: this has not been exercised against a live 3x-ui v3.6.0 panel
(see PLAN.md Etap 0 R&D) -- treat it as needing that verification pass
before relying on it in production.
"""

from __future__ import annotations

import json

REALITY_PORT = 443

# Oldest xray-core a client may run and still be accepted.
#
# This MUST be set explicitly. Left unset, xray-core does not mean "any
# version" -- infra/conf/transport_security.go falls through to
#
#     config.MinClientVer = []byte{26, 3, 27}
#     LogWarning("REALITY: The default minimal client version is Xray-core
#                 v26.3.27, other clients may be refused to connect")
#
# and refuses every client on an older core. That is nearly every real app
# (v2rayNG, Hiddify, Streisand, NekoBox...), which presents as a config that
# looks perfect and simply never connects.
#
# 1.8.0 is where REALITY + xtls-rprx-vision stabilised, so it accepts every
# client in practical use. The tradeoff xray warns about is real: a lower
# floor lets an old or forged client probe the server, which is what the
# high default is defending against. Being connectable wins here -- a VPN
# nobody's client can reach is not a hardened VPN.
MIN_CLIENT_VERSION = "1.8.0"


def build_reality_vless_inbound_payload(
    *, sni: str, private_key: str, short_id: str, remark: str, port: int = REALITY_PORT
) -> dict:
    settings = {
        "clients": [],
        "decryption": "none",
        "fallbacks": [],
    }
    stream_settings = {
        "network": "tcp",
        "security": "reality",
        "realitySettings": {
            "show": False,
            "dest": f"{sni}:443",
            "xver": 0,
            "serverNames": [sni],
            "privateKey": private_key,
            "minClientVer": MIN_CLIENT_VERSION,
            "shortIds": [short_id],
            "settings": {
                "publicKey": "",  # server side does not need its own public key
                "fingerprint": "chrome",
                "spiderX": "/",
            },
        },
        "tcpSettings": {"header": {"type": "none"}},
    }
    sniffing = {
        "enabled": True,
        "destOverride": ["http", "tls", "quic"],
    }

    return {
        "up": 0,
        "down": 0,
        "total": 0,
        "remark": remark,
        "enable": True,
        "expiryTime": 0,
        "listen": "",
        "port": port,
        "protocol": "vless",
        "settings": json.dumps(settings),
        "streamSettings": json.dumps(stream_settings),
        "sniffing": json.dumps(sniffing),
    }
