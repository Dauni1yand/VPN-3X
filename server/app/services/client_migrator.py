"""Moves an existing client from one node to another (README: "переброс
пользователя с одной ноды на другую") -- used to drain a node for
maintenance or once it's flagged unstable, without cutting the user's
remaining VPN time short.

Under 3x-ui this meant creating a client on the target node, then deleting
the one on the source, in that order so a failure on a half-dead source
node never left the user without access.

Under Remnawave it is one call. A user's reach is their squad membership,
so moving them between nodes is swapping which squad they are in -- the
same user, the same subscription, the same remaining expiry. There is no
window where they hold two credentials or none, which is what the careful
ordering used to be defending against.
"""

from __future__ import annotations

from app.db.models import Client, Inbound, Node
from app.services.remnawave_client import RemnawaveError, get_remnawave
from app.services.vless import pick_reality_link


async def migrate_client(
    client: Client,
    old_inbound: Inbound,  # noqa: ARG001 -- kept for call-site symmetry with the target side
    old_node: Node,  # noqa: ARG001 -- the swap is expressed as the new squad, not a removal
    target_inbound: Inbound,
    target_node: Node,
) -> None:
    if not client.remnawave_user_uuid:
        raise RemnawaveError(
            "у клиента нет пользователя в панели Remnawave — "
            "он выдан ещё под 3x-ui, выдайте конфиг заново"
        )

    if not target_node.internal_squad_uuid:
        raise RemnawaveError("у целевой ноды нет internal squad в панели")

    panel = get_remnawave()

    await panel.update_user(
        client.remnawave_user_uuid,
        activeInternalSquads=[target_node.internal_squad_uuid],
    )

    client.inbound_id = target_inbound.id

    # The stored link points at the node we just moved off, so it has to be
    # re-read from the panel rather than left to go stale -- "Мой конфиг"
    # serves this string directly.
    client.vless_uri = await pick_reality_link(
        panel,
        client.remnawave_short_uuid or "",
        target_node,
        target_inbound,
        client.remote_client_uuid,
        remark="vpn-3x",
    )
