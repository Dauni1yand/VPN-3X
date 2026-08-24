"""Turns a Remnawave user into the vless:// link we hand out.

The link is asked of the panel, never composed here. That was settled while
this project still ran on 3x-ui, and the reasoning did not depend on which
panel it was: composing the URI on the main server means keeping a second
implementation of the share-link format in sync with the one the node is
actually configured from, and every field that drifts produces a config
that connects, reports a latency and carries no traffic. REALITY answers a
refused handshake by proxying to `dest`, so drift is invisible from the
client side and indistinguishable from a working config.

build_vless_uri survives only as the fallback for when the panel cannot
answer -- and for rows issued under 3x-ui, whose links it still describes.
"""

from __future__ import annotations

import logging
from urllib.parse import parse_qs, quote, urlparse

from app.db.models import Inbound, Node

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

    Remnawave fills it from its own template, which can carry the username;
    ours are `<telegram_id>-<hex>`, so leaving it would print the user's
    Telegram id on the config card in their app.
    """

    return f"{uri.split('#', 1)[0]}#{quote(remark)}"


def _has_reality_params(uri: str) -> bool:
    """Whether a link carries the parameters a REALITY client needs.

    Guards against handing out a link that connects and carries no traffic.
    An empty `pbk=` is the specific way a panel's generator fails when the
    public half of the keypair is missing from the config it reads.
    """

    params = parse_qs(urlparse(uri).query, keep_blank_values=True)
    return all(params.get(key, [""])[0] for key in ("pbk", "sid", "sni"))


async def pick_reality_link(
    panel, short_uuid: str, node: Node, inbound: Inbound, client_uuid: str, remark: str
) -> str:
    """The user's REALITY link, from the panel, with our own remark.

    A Remnawave subscription can hold several links; ours holds one,
    because the user is scoped to a single node's squad. Still filtered
    rather than assumed: taking links[0] would silently hand out whatever
    happened to be first if that ever stops being true.
    """

    try:
        links = await panel.get_subscription_links(short_uuid) if short_uuid else []
    except Exception as exc:  # noqa: BLE001 -- fall back, don't lose the config
        logger.warning(
            "panel could not generate links for %s (%s: %s); composing locally",
            short_uuid,
            type(exc).__name__,
            exc,
        )
        links = []

    for link in links:
        if not link.startswith("vless://"):
            continue
        if urlparse(link).port != inbound.port:
            continue
        if not _has_reality_params(link):
            logger.warning(
                "panel returned a link without complete REALITY parameters (%s)",
                link.split("#", 1)[0],
            )
            continue
        return _replace_remark(link, remark)

    logger.warning(
        "no usable REALITY link among %d returned for %s; composing locally",
        len(links),
        short_uuid,
    )
    return build_vless_uri(node, inbound, client_uuid, remark)
