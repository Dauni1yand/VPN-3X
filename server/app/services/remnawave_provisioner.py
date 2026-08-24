"""Builds a node's REALITY inbound as a Remnawave config profile.

Under 3x-ui the inbound was created on the node's own panel. Under
Remnawave the inbound lives in a **config profile** -- an Xray config held
centrally -- and the panel pushes it to whichever nodes are bound to that
profile.

We give every node its own profile and its own internal squad rather than
sharing one across the fleet. Sharing would be tidier, but it would cost
the thing this project is built on: with one profile the panel hands every
user every node, and the *client app* picks. A profile and a squad per node
keeps the choice on the server, where the balancer can make it.

Two things carry over unchanged from the 3x-ui work, because they were
never about the panel:

  * the SNI is probed for real (sni_prober) rather than hardcoded, and
  * minClientVer is set explicitly. Left unset, xray-core does not mean
    "any version" -- it falls through to v26.3.27 and refuses every client
    on an older core, which is nearly every real app.

One thing does NOT carry over: 3x-ui took settings/streamSettings as
JSON-encoded strings, a quirk of its Go models. Remnawave takes a normal
Xray config, so these are nested objects.
"""

from __future__ import annotations

from app.core.security import decrypt_secret, encrypt_secret
from app.db.models import Inbound, Node
from app.services.reality_inbound import MIN_CLIENT_VERSION, REALITY_PORT
from app.services.reality_keys import generate_reality_keypair, generate_short_id
from app.services.remnawave_client import RemnawaveError, get_remnawave
from app.services.sni_prober import pick_working_sni


class InboundNotFoundError(RuntimeError):
    """The profile was created but the panel did not report our inbound."""


def inbound_tag(node: Node) -> str:
    """The inbound's tag inside the profile.

    Also how we find the inbound again: the panel assigns its UUID, so the
    tag is the only handle we control, and it has to survive a profile
    update without changing.
    """

    return f"VPN3X_REALITY_{node.id.replace('-', '')[:12].upper()}"


def build_profile_config(
    *, sni: str, private_key: str, public_key: str, short_id: str, tag: str, port: int = REALITY_PORT
) -> dict:
    """A complete Xray config holding one VLESS+REALITY+vision inbound.

    `clients` stays empty: Remnawave injects the users of whichever squads
    reference this inbound. Writing users in here by hand would be
    overwritten on the next push.
    """

    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "tag": tag,
                "port": port,
                "protocol": "vless",
                "listen": "0.0.0.0",
                "settings": {"clients": [], "decryption": "none"},
                "streamSettings": {
                    "network": "tcp",
                    "security": "reality",
                    "realitySettings": {
                        "show": False,
                        "dest": f"{sni}:443",
                        "xver": 0,
                        "serverNames": [sni],
                        "privateKey": private_key,
                        # Harmless to xray, which derives it from privateKey.
                        # Kept because a panel that generates share links
                        # needs the public half from somewhere, and the one
                        # thing this project has already been bitten by is a
                        # link generator reading pbk out of the config and
                        # finding it blank.
                        "publicKey": public_key,
                        "minClientVer": MIN_CLIENT_VERSION,
                        "shortIds": [short_id],
                    },
                },
                "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"]},
            }
        ],
        # A node with no freedom outbound accepts the connection and drops
        # the traffic, which looks to the client exactly like a working
        # config. Spelled out here rather than left to a panel default.
        "outbounds": [
            {"tag": "DIRECT", "protocol": "freedom"},
            {"tag": "BLOCK", "protocol": "blackhole"},
        ],
        "routing": {"rules": []},
    }


async def provision_node_profile(node: Node, *, sni: str | None = None) -> Inbound:
    """Creates the node's config profile and its internal squad.

    Returns an unsaved Inbound row; the caller adds and commits it together
    with whatever else that transaction owns.
    """

    panel = get_remnawave()

    sni = sni or await pick_working_sni()
    private_key, public_key = generate_reality_keypair()
    short_id = generate_short_id()
    tag = inbound_tag(node)

    config = build_profile_config(
        sni=sni, private_key=private_key, public_key=public_key, short_id=short_id, tag=tag
    )

    profile = await panel.create_config_profile(_profile_name(node), config)
    profile_uuid = str(profile["uuid"])

    inbound_uuid = await _find_inbound_uuid(panel, profile_uuid, tag)

    # One squad per node, holding only this node's inbound. This is what
    # lets the balancer decide: a user in this squad reaches this node and
    # no other.
    squad = await panel.create_internal_squad(_squad_name(node), [inbound_uuid])

    node.config_profile_uuid = profile_uuid
    node.internal_squad_uuid = str(squad["uuid"])
    node.sni = sni

    return Inbound(
        node_id=node.id,
        remnawave_inbound_uuid=inbound_uuid,
        port=REALITY_PORT,
        sni=sni,
        reality_public_key=public_key,
        reality_private_key_encrypted=encrypt_secret(private_key),
        reality_short_id=short_id,
    )


async def rotate_profile_sni(node: Node, inbound: Inbound) -> str:
    """Re-probes for a working SNI and swaps it in place.

    The keypair and shortId are deliberately reused, so configs already in
    circulation keep working -- only the impersonated destination changes.
    Rewriting the whole config is safe here in a way it was not under 3x-ui:
    there is no client list in it to wipe, because Remnawave owns that.
    """

    panel = get_remnawave()

    if not node.config_profile_uuid:
        raise RemnawaveError("у ноды нет config profile в панели")

    new_sni = await pick_working_sni()
    private_key = decrypt_secret(inbound.reality_private_key_encrypted)

    await panel.update_config_profile(
        node.config_profile_uuid,
        config=build_profile_config(
            sni=new_sni,
            private_key=private_key,
            public_key=inbound.reality_public_key or "",
            short_id=inbound.reality_short_id or "",
            tag=inbound_tag(node),
            port=inbound.port,
        ),
    )

    return new_sni


async def teardown_node_profile(node: Node) -> None:
    """Removes the squad and profile a node owned.

    Best-effort per object: a squad that is already gone must not stop the
    profile being cleaned up, or deleting a half-provisioned node becomes
    impossible without going into the panel by hand.
    """

    panel = get_remnawave()

    for delete, uuid in (
        (panel.delete_internal_squad, node.internal_squad_uuid),
        (panel.delete_config_profile, node.config_profile_uuid),
    ):
        if not uuid:
            continue
        try:
            await delete(uuid)
        except RemnawaveError:
            pass


async def _find_inbound_uuid(panel, profile_uuid: str, tag: str) -> str:
    inbounds = await panel.get_profile_inbounds(profile_uuid)

    for entry in inbounds:
        if entry.get("tag") == tag:
            return str(entry["uuid"])

    raise InboundNotFoundError(
        f"панель приняла профиль, но не показала инбаунд {tag} "
        f"(получено: {[entry.get('tag') for entry in inbounds]})"
    )


def _profile_name(node: Node) -> str:
    # The panel validates ^[A-Za-z0-9_\s-]{2,30}$, and node names are
    # admin-entered free text, so build the name from the id instead.
    return f"vpn3x-{node.id.replace('-', '')[:16]}"


def _squad_name(node: Node) -> str:
    return f"vpn3x-sq-{node.id.replace('-', '')[:14]}"
