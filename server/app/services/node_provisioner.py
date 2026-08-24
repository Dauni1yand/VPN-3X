"""Creates and maintains the VPN-3X REALITY inbound."""

from __future__ import annotations

import json

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


class InboundPortInUseError(RuntimeError):
    """The target port is held by an inbound that has users on it."""


def _clients_of(inbound: dict) -> list:
    """The client list of an inbound as the panel reports it.

    `settings` normally comes back as a nested object (Inbound.MarshalJSON
    expands it on the way out) but falls back to a JSON string when the
    stored text isn't valid JSON, so both shapes have to be handled.
    """

    settings = inbound.get("settings")

    if isinstance(settings, str):
        try:
            settings = json.loads(settings)
        except ValueError:
            return []

    if not isinstance(settings, dict):
        return []

    clients = settings.get("clients")

    return clients if isinstance(clients, list) else []


def _client_count(inbound: dict) -> int:
    return len(_clients_of(inbound))


def _settings_with_clients(settings: str, clients: list) -> str:
    """Puts `clients` into a JSON-encoded `settings` string.

    The inbound API takes settings as a string, so swapping one field means
    decoding and re-encoding rather than mutating a dict.
    """

    decoded = json.loads(settings)
    decoded["clients"] = clients
    return json.dumps(decoded)


async def provision_default_inbound(
    node: Node,
    *,
    sni: str | None = None,
    takeover: bool = False,
) -> Inbound:
    """Gives `node` its VLESS+REALITY inbound on tcp/443.

    `takeover` says whether an inbound already on that port may be claimed
    even when it has clients on it. The two callers differ genuinely:

      * bootstrap (takeover=True) has just run the 3x-ui installer on this
        box over SSH and forced its panel credentials, port and base path.
        It has already assumed ownership of the machine, so balking at a
        leftover inbound -- usually from an earlier bootstrap of the same
        box, since /etc/x-ui/x-ui.db survives a re-install -- would be
        inconsistent with everything it just did.

      * the manual "создать инбаунд" button (takeover=False) is typically
        used on a panel that was connected, not installed, by us. Its
        clients may be real users, and cutting them off silently is not
        ours to do.
    """


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
            public_key=public_key,
            short_id=short_id,
            remark=f"vpn-3x-{node.name}",
        )
    )

    threexui = get_pooled_client(node)

    # Doubles as a check that authentication and the API itself work.
    existing_inbounds = await threexui.list_inbounds()

    port = int(payload["port"])

    # 3x-ui refuses to add an inbound onto a port another one already holds
    # ("port 443 (tcp) already used by inbound 'in-443-tcp' (#1)"), and tcp/443
    # is not negotiable for us -- it is the port the README pins REALITY to.
    # A node that has been bootstrapped before, or that 3x-ui seeded itself,
    # arrives with something already sitting there.
    occupying = next(
        (
            candidate
            for candidate in existing_inbounds
            if isinstance(candidate, dict) and candidate.get("port") == port
        ),
        None,
    )

    if occupying is not None:
        clients = _client_count(occupying)

        if clients and not takeover:
            # Someone's users live on it. Overwriting would cut them off
            # silently, so make the admin decide instead.
            raise InboundPortInUseError(
                f"port {port} on this node is already used by inbound "
                f"{occupying.get('remark') or occupying.get('tag') or occupying.get('id')} "
                f"which has {clients} client(s). Remove it in the 3x-ui panel "
                f"first if this node should be managed by VPN-3X."
            )

        # Take it over in place rather than delete-then-create, which would
        # leave the node with no inbound at all if the create leg failed.
        # Any clients on it go with the old REALITY keys: their configs stop
        # working either way once the keypair is replaced, so keeping the
        # rows would only be misleading.
        remote_id = int(occupying["id"])
        await threexui.update_inbound(remote_id, payload)

        return Inbound(
            node_id=node.id,
            remote_inbound_id=remote_id,
            protocol="vless",
            transport="tcp",
            port=port,
            sni=sni,
            reality_public_key=public_key,
            reality_private_key_encrypted=(
                encrypt_secret(private_key)
            ),
            reality_short_id=short_id,
        )

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
            public_key=inbound.reality_public_key,
            short_id=inbound.reality_short_id,
            remark=f"vpn-3x-{node.name}",
            port=inbound.port,
        )
    )

    threexui = get_pooled_client(node)

    # 3x-ui's update writes `settings` through verbatim, so posting the
    # freshly built payload as-is would replace the client list with the
    # empty one the builder starts from -- silently deleting every user on
    # the inbound. Rotating the SNI is supposed to leave them in place (only
    # the dest changes; the keypair and shortId are reused above precisely so
    # existing configs keep working), so carry the node's current clients over.
    current = await threexui.get_inbound(inbound.remote_inbound_id)
    payload["settings"] = _settings_with_clients(
        payload["settings"], _clients_of(current)
    )

    await threexui.update_inbound(
        inbound.remote_inbound_id,
        payload,
    )

    return new_sni