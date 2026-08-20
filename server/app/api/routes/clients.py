from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_internal_api_key
from app.db.models import Client, Inbound, Node
from app.db.session import get_db
from app.schemas.clients import ClientCreate, ClientMigrate, ClientOut
from app.services.audit import log_admin_action
from app.services.client_issuer import issue_client
from app.services.client_migrator import migrate_client
from app.services.node_balancer import pick_node_for_client
from app.services.vless import build_vless_uri

router = APIRouter(prefix="/clients", tags=["clients"], dependencies=[Depends(require_internal_api_key)])


@router.post("", response_model=ClientOut, status_code=status.HTTP_201_CREATED)
async def create_client(
    payload: ClientCreate,
    db: AsyncSession = Depends(get_db),
    cf_ip_country: str | None = Header(default=None, alias="CF-IPCountry"),
) -> ClientOut:
    return await issue_client(
        db,
        payload.user_telegram_id,
        payload.duration_seconds,
        client_country=cf_ip_country,
        client_latencies=payload.client_latencies,
    )


@router.post("/{client_id}/migrate", response_model=ClientOut)
async def migrate_client_route(
    client_id: str, payload: ClientMigrate, db: AsyncSession = Depends(get_db)
) -> ClientOut:
    """Moves a client to another node without cutting its remaining time
    short (README: "переброс пользователя с одной ноды на другую") -- for
    draining a node ahead of maintenance, or getting a user off one that
    just flipped unstable."""

    client = await db.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="client not found")

    old_inbound = await db.get(Inbound, client.inbound_id)
    old_node = await db.get(Node, old_inbound.node_id)

    if payload.target_node_id is not None:
        target_node = await db.get(Node, payload.target_node_id)
        if target_node is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="target node not found")
    else:
        target_node = await pick_node_for_client(db, exclude_node_id=old_node.id)
        if target_node is None:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="no other active node available")

    if target_node.id == old_node.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="target node is the same as the current one")

    target_inbound = (
        await db.execute(select(Inbound).where(Inbound.node_id == target_node.id).limit(1))
    ).scalar_one_or_none()
    if target_inbound is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="target node has no inbound provisioned")

    await migrate_client(client, old_inbound, old_node, target_inbound, target_node)

    log_admin_action(
        db,
        admin_telegram_id=payload.admin_telegram_id,
        action="migrate_client",
        target=client.id,
        details=f"{old_node.id} -> {target_node.id}",
    )
    await db.commit()
    await db.refresh(client)

    vless_uri = build_vless_uri(target_node, target_inbound, client.remote_client_uuid, remark="vpn-3x")
    return ClientOut(id=client.id, status=client.status, expires_at=client.expires_at, vless_uri=vless_uri)
