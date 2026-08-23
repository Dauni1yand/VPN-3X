"""Creates and maintains the VPN-3X REALITY inbound."""

from __future__ import annotations

from app.core.security import (
    decrypt_secret,
    encrypt_secret,
)

from app.db.models import Inbound, Node

from app.services.reality_inbound import (
    build_reality_vless_inbound_payload,
)

from app.services.reality_keys import (
    generate_reality_keypair,
    generate_short_id,
)

from app.services.sni_prober import (
    pick_working_sni,
)

from app.services.threexui_client import (
    get_pooled_client,
)


async def provision_default_inbound(
    node: Node,
    *,
    sni: str | None = None,
) -> Inbound:

    if sni is None:
        sni = await pick_working_sni()

    private_key, public_key = (
        generate_reality_keypair()
    )

    short_id = generate_short_id()

    payload = (
        build_reality_vless_inbound_payload(
            sni=sni,
            private_key=private_key,
            short_id=short_id,
            remark=f"vpn-3x-{node.name}",
        )
    )

    threexui = get_pooled_client(node)

    # First make sure authentication and the API itself work.
    await threexui.list_inbounds()

    result = await threexui.add_inbound(
        payload
    )

    remote_id = result.get("id")

    if remote_id is None:
        raise RuntimeError(
            f"3x-ui created an invalid inbound: {result}"
        )

    return Inbound(
        node_id=node.id,
        remote_inbound_id=int(remote_id),
        protocol="vless",
        transport="tcp",
        port=payload["port"],
        sni=sni,
        reality_public_key=public_key,
        reality_private_key_encrypted=(
            encrypt_secret(private_key)
        ),
        reality_short_id=short_id,
    )


async def rotate_inbound_sni(
    node: Node,
    inbound: Inbound,
) -> str:

    new_sni = await pick_working_sni()

    private_key = decrypt_secret(
        inbound.reality_private_key_encrypted
    )

    payload = (
        build_reality_vless_inbound_payload(
            sni=new_sni,
            private_key=private_key,
            short_id=inbound.reality_short_id,
            remark=f"vpn-3x-{node.name}",
            port=inbound.port,
        )
    )

    threexui = get_pooled_client(node)

    await threexui.update_inbound(
        inbound.remote_inbound_id,
        payload,
    )

    return new_sni