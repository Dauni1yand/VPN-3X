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
