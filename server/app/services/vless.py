"""Turns a client into a vless:// share URI.

Two paths. resolve_vless_uri asks the node's own 3x-ui to generate the link
and is what callers should use: the panel generates it from the same inbound
row xray is configured from, so it cannot disagree with what the node
actually serves. build_vless_uri composes the URI here from our Inbound row
and remains the fallback for when the panel cannot answer.

See https://xtls.github.io/config/features/reality.html for the parameter
meanings (pbk, sid, fp, spx).
"""

from __future__ import annotations

import logging
from urllib.parse import parse_qs, quote, urlparse

from app.db.models import Inbound, Node
from app.services.threexui_client import get_pooled_client

logger = logging.getLogger(__name__)


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


def _replace_remark(uri: str, remark: str) -> str:
    """Swaps the URI's `#fragment` -- the name the client app displays.

    The node's generator fills this from the panel's global remarkTemplate,
    which defaults to `{{INBOUND}}-{{EMAIL}}|<traffic>|<days>`. Our emails are
    `<telegram_id>-<uuid8>`, so leaving it would print the user's Telegram id
    on the config card in their app, and bake a traffic/days figure that is
    only correct at the moment the link was issued. The name is cosmetic and
    carries no connection parameter, so overriding it costs nothing.
    """

    base = uri.split("#", 1)[0]
    return f"{base}#{quote(remark)}"


def _has_reality_params(uri: str) -> bool:
    """Whether a link carries the parameters a REALITY client needs.

    Guards against silently handing out a link that connects and carries no
    traffic. An empty `pbk=` is the specific way the panel's generator fails:
    it reads the public key from realitySettings.settings.publicKey, which is
    blank on any inbound created before we started filling it in.
    """

    query = urlparse(uri).query
    params = parse_qs(query, keep_blank_values=True)
    return all(params.get(key, [""])[0] for key in ("pbk", "sid", "sni"))


async def resolve_vless_uri(
    node: Node,
    inbound: Inbound,
    client_uuid: str,
    email: str,
    remark: str,
) -> str:
    """The client's share link, preferring the one the node generates itself.

    Composing the URI here means keeping a second implementation of 3x-ui's
    link format in sync with the node's, and every field that drifts produces
    a config that connects, reports a latency and moves no traffic -- REALITY
    answers a refused handshake by proxying to `dest`, so drift is invisible
    from the client side. Asking the node removes the second implementation
    from the path that matters.

    It does not remove it from the codebase: build_vless_uri stays as the
    fallback. Issuance already depends on the panel (the client has to be
    added there first), so this adds no new failure mode to it, but a panel
    that answers `add` and then fails `links` would otherwise lose a config
    the user has already paid for.
    """

    try:
        links = await get_pooled_client(node).get_client_links(email)
    except Exception as exc:  # noqa: BLE001 -- fall back, don't lose the config
        logger.warning(
            "node %s could not generate a link for %s (%s: %s); composing it locally",
            node.id,
            email,
            type(exc).__name__,
            exc,
        )
        return build_vless_uri(node, inbound, client_uuid, remark)

    for link in links:
        if not link.startswith(f"vless://{client_uuid}@"):
            continue
        if not _has_reality_params(link):
            logger.warning(
                "node %s generated a link for %s without complete REALITY "
                "parameters (%s); composing it locally instead",
                node.id,
                email,
                link.split("#", 1)[0],
            )
            break
        return _replace_remark(link, remark)
    else:
        logger.warning(
            "node %s returned %d link(s) for %s, none for uuid %s; composing locally",
            node.id,
            len(links),
            email,
            client_uuid,
        )

    return build_vless_uri(node, inbound, client_uuid, remark)
