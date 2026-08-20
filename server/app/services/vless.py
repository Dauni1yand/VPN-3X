"""Builds a vless:// share URI for a REALITY+XTLS-vision client.

See https://xtls.github.io/config/features/reality.html for the parameter
meanings (pbk, sid, fp, spx). Values come from the node's Inbound row, which
must be populated when the inbound is provisioned (Etap 1) -- not guessed
here.
"""

from __future__ import annotations

from urllib.parse import quote

from app.db.models import Inbound, Node


def build_vless_uri(node: Node, inbound: Inbound, client_uuid: str, remark: str) -> str:
    if not inbound.reality_public_key or not inbound.reality_short_id or not inbound.sni:
        raise ValueError("inbound is missing REALITY parameters (pbk/sid/sni)")

    params = {
        "type": "tcp" if inbound.transport == "tcp" else "grpc",
        "security": "reality",
        "pbk": inbound.reality_public_key,
        "sid": inbound.reality_short_id,
        "sni": inbound.sni,
        "fp": "chrome",
        "flow": "xtls-rprx-vision" if inbound.transport == "tcp" else "",
    }
    query = "&".join(f"{k}={quote(str(v))}" for k, v in params.items() if v)
    return f"vless://{client_uuid}@{node.ip}:{inbound.port}?{query}#{quote(remark)}"
