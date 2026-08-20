"""Moves an existing client from one node to another (README: "переброс
пользователя с одной ноды на другую") -- used to drain a node for
maintenance or once it's flagged unstable, without cutting the user's
remaining VPN time short.

Issues a fresh client on the target inbound for the SAME remaining expiry
first, and only then deletes the old one -- in that order, so a failure
deleting the old client (the node might already be half-dead, which is
often *why* we're migrating off it) never leaves the user without access."""

from __future__ import annotations

import uuid

from app.db.models import Client, Inbound, Node
from app.services.threexui_client import get_pooled_client


async def migrate_client(
    client: Client, old_inbound: Inbound, old_node: Node, target_inbound: Inbound, target_node: Node
) -> None:
    new_uuid = str(uuid.uuid4())
    new_email = f"{client.email.rsplit('-', 1)[0]}-{new_uuid[:8]}"

    target_threexui = get_pooled_client(target_node)
    await target_threexui.add_client(
        inbound_id=target_inbound.remote_inbound_id,
        client_settings={
            "clients": [
                {
                    "id": new_uuid,
                    "email": new_email,
                    "flow": "xtls-rprx-vision",
                    "expiryTime": int(client.expires_at.timestamp() * 1000),
                }
            ]
        },
    )

    old_threexui = get_pooled_client(old_node)
    try:
        await old_threexui.delete_client(old_inbound.remote_inbound_id, client.remote_client_uuid)
    except Exception:  # noqa: BLE001 -- best-effort cleanup; the new client is already
        # active on the target node by this point, so a stale leftover on a
        # node we're migrating away from (often because it's unhealthy) must
        # not fail the migration itself.
        pass

    client.inbound_id = target_inbound.id
    client.remote_client_uuid = new_uuid
    client.email = new_email
