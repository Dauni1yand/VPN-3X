"""Orchestrates turning a freshly-added Node into one with a working
VLESS+REALITY+XTLS inbound (README: "настраивать с нуля ноды, делать
инбаунды, автоматически находить хороший sni"), and rotating an inbound's
SNI in place when its current one gets blocked (README: admin triggers this
by hand from the bot).

This assumes the node already has 3x-ui installed and reachable at
`panel_base_url` -- actually installing 3x-ui onto a bare Ubuntu box over
SSH is separate infrastructure work, not covered here yet."""

from __future__ import annotations

from app.core.security import decrypt_secret, encrypt_secret
from app.db.models import Inbound, Node
from app.services.reality_inbound import build_reality_vless_inbound_payload
from app.services.reality_keys import generate_reality_keypair, generate_short_id
from app.services.sni_prober import pick_working_sni
from app.services.threexui_client import ThreeXUIClient


def _node_client(node: Node) -> ThreeXUIClient:
    return ThreeXUIClient(
        base_url=node.panel_base_url,
        login=node.panel_login,
        password=decrypt_secret(node.panel_password_encrypted),
    )


async def provision_default_inbound(node: Node) -> Inbound:
    """Creates the node's first (and, per README, only) inbound and returns
    the Inbound row to persist. Does not commit -- the caller owns the
    session/transaction."""

    sni = await pick_working_sni()
    private_key, public_key = generate_reality_keypair()
    short_id = generate_short_id()

    payload = build_reality_vless_inbound_payload(
        sni=sni, private_key=private_key, short_id=short_id, remark=f"vpn-3x-{node.name}"
    )

    threexui = _node_client(node)
    try:
        result = await threexui.add_inbound(payload)
    finally:
        await threexui.aclose()

    return Inbound(
        node_id=node.id,
        remote_inbound_id=result["id"],
        protocol="vless",
        transport="tcp",
        port=payload["port"],
        sni=sni,
        reality_public_key=public_key,
        reality_private_key_encrypted=encrypt_secret(private_key),
        reality_short_id=short_id,
    )


async def rotate_inbound_sni(node: Node, inbound: Inbound) -> str:
    """Re-probes for a working SNI and updates the inbound's REALITY dest in
    place, keeping the existing keypair/shortId and clients untouched.
    Returns the new SNI; the caller is responsible for persisting
    `inbound.sni = new_sni` and committing.

    Note: any VLESS URI already handed to a user encodes the old SNI, so it
    stops working once this runs -- that's inherent to REALITY (the client
    validates the server's certificate is for that SNI), not something this
    function can avoid. Rotating SNI is meant for a node whose current SNI
    got blocked, where old configs are already dead anyway; affected users
    need a fresh config afterwards."""

    new_sni = await pick_working_sni()
    private_key = decrypt_secret(inbound.reality_private_key_encrypted)

    payload = build_reality_vless_inbound_payload(
        sni=new_sni,
        private_key=private_key,
        short_id=inbound.reality_short_id,
        remark=f"vpn-3x-{node.name}",
        port=inbound.port,
    )

    threexui = _node_client(node)
    try:
        await threexui.update_inbound(inbound.remote_inbound_id, payload)
    finally:
        await threexui.aclose()

    return new_sni
