"""Grants a user VPN access for a fixed length of time.

Under 3x-ui this meant adding a client to one node's inbound. Under
Remnawave it means creating a *user*, because that is the object the panel
models -- a person with an expiry and a set of squads, not a per-node
credential.

That difference would quietly cost this project something if taken at face
value. A Remnawave user's reach is the union of their squads, and their
subscription hands the client app every node in it to choose between. Here
the server chooses, so a user is put in exactly one node's squad: the one
the balancer picked. Moving them between nodes later is a squad swap.
"""

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Client, ClientStatus, Inbound, Node, NodeStatus
from app.schemas.clients import ClientOut
from app.services.node_balancer import pick_node_for_client
from app.services.remnawave_client import RemnawaveError, get_remnawave
from app.services.users import get_or_create_user
from app.services.vless import pick_reality_link


async def issue_client(
    db: AsyncSession,
    telegram_id: int,
    duration_seconds: int,
    *,
    client_country: str | None = None,
    client_latencies: dict[str, float] | None = None,
    target_node_id: str | None = None,
) -> ClientOut:
    """Grants `telegram_id` VPN access valid for `duration_seconds`, whether
    that time was earned by watching an ad or by a paid subscription.
    Shared by /clients (direct) and /subscriptions (ad-view / payment).

    `client_country` / `client_latencies` feed the node balancer's
    connection-quality estimate -- see node_balancer.py for how they're used
    and what happens when neither is available.

    `target_node_id` bypasses the balancer entirely -- regular users never
    get to pick their node (README requirement), but the admin-issued-config
    path does explicitly choose one, e.g. to test a specific node.

    Does NOT commit -- the caller adds whatever idempotency-guard row goes
    with the reason it's issuing a client (a Payment or an AdView) and
    commits both in one transaction. Committing here instead would let two
    concurrent requests for the same invoice/impression both pass their
    "not already credited" check, each mint a client, and only then collide
    on the guard row's unique constraint -- leaving one paid-for client
    granted for free with no way to roll it back.
    """

    user = await get_or_create_user(db, telegram_id)

    if target_node_id is not None:
        node = await db.get(Node, target_node_id)
        if node is None or node.status != NodeStatus.active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="target node not found or not active",
            )
    else:
        node = await pick_node_for_client(
            db, client_country=client_country, client_latencies=client_latencies
        )
    if node is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="no active node available"
        )

    inbound = (
        await db.execute(select(Inbound).where(Inbound.node_id == node.id).limit(1))
    ).scalar_one_or_none()
    if inbound is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="node has no inbound provisioned",
        )

    if not node.internal_squad_uuid:
        # Without a squad there is nothing to scope the user to, and
        # creating them anyway would produce a user who can reach no node
        # at all -- a subscription that resolves to an empty list.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="у ноды нет internal squad в панели, переустановите ноду",
        )

    # Remnawave validates ^[a-zA-Z0-9_-]{3,36}$ on usernames, which this
    # fits, and it has to stay unique across the panel -- hence the random
    # tail rather than the bare telegram id.
    username = f"{telegram_id}-{uuid.uuid4().hex[:8]}"
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)

    panel = get_remnawave()

    try:
        created = await panel.create_user(
            username=username,
            expire_at=expires_at.isoformat().replace("+00:00", "Z"),
            internal_squads=[node.internal_squad_uuid],
            telegram_id=telegram_id,
            description=f"vpn-3x node={node.name}",
        )
    except RemnawaveError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"панель не смогла создать пользователя: {exc}",
        ) from exc

    short_uuid = str(created.get("shortUuid") or "")
    client_uuid = str(created.get("vlessUuid") or created.get("uuid"))
    vless_uri = await pick_reality_link(
        panel, short_uuid, node, inbound, client_uuid, remark="vpn-3x"
    )

    client = Client(
        inbound_id=inbound.id,
        user_id=user.id,
        remote_client_uuid=client_uuid,
        email=username,
        status=ClientStatus.active,
        expires_at=expires_at,
        remnawave_user_uuid=str(created["uuid"]),
        remnawave_short_uuid=short_uuid,
        subscription_url=created.get("subscriptionUrl"),
        vless_uri=vless_uri,
    )

    db.add(client)
    await db.flush()

    return ClientOut(
        id=client.id,
        status=client.status,
        expires_at=client.expires_at,
        vless_uri=vless_uri,
        subscription_url=client.subscription_url,
    )
