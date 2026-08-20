from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_internal_api_key
from app.core.security import encrypt_secret
from app.db.models import Inbound, Node, NodeStatus
from app.db.session import get_db
from app.schemas.inbounds import InboundOut
from app.schemas.nodes import NodeCreate, NodeCredentialsUpdate, NodeOut
from app.services.node_provisioner import provision_default_inbound, rotate_inbound_sni

router = APIRouter(prefix="/nodes", tags=["nodes"], dependencies=[Depends(require_internal_api_key)])


@router.post("", response_model=NodeOut, status_code=status.HTTP_201_CREATED)
async def create_node(payload: NodeCreate, db: AsyncSession = Depends(get_db)) -> Node:
    node = Node(
        name=payload.name,
        ip=payload.ip,
        panel_base_url=payload.panel_base_url,
        panel_login=payload.panel_login,
        panel_password_encrypted=encrypt_secret(payload.panel_password),
        country=payload.country.upper() if payload.country else None,
    )
    db.add(node)
    await db.commit()
    await db.refresh(node)
    return node


@router.get("", response_model=list[NodeOut])
async def list_nodes(db: AsyncSession = Depends(get_db)) -> list[Node]:
    result = await db.execute(select(Node).order_by(Node.created_at))
    return list(result.scalars())


@router.patch("/{node_id}/credentials", response_model=NodeOut)
async def update_node_credentials(
    node_id: str, payload: NodeCredentialsUpdate, db: AsyncSession = Depends(get_db)
) -> Node:
    node = await db.get(Node, node_id)
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="node not found")

    if payload.panel_login:
        node.panel_login = payload.panel_login
    if payload.panel_password:
        node.panel_password_encrypted = encrypt_secret(payload.panel_password)

    await db.commit()
    await db.refresh(node)
    return node


@router.delete("/{node_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_node(node_id: str, db: AsyncSession = Depends(get_db)) -> None:
    node = await db.get(Node, node_id)
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="node not found")
    await db.delete(node)
    await db.commit()


@router.post("/{node_id}/inbound", response_model=InboundOut, status_code=status.HTTP_201_CREATED)
async def provision_inbound(node_id: str, db: AsyncSession = Depends(get_db)) -> Inbound:
    """Auto-provisions the node's VLESS+REALITY+XTLS inbound: probes for a
    working SNI, generates a fresh REALITY keypair, and creates the inbound
    on the node's 3x-ui panel (README: "настраивать с нуля ноды, делать
    инбаунды, автоматически находить хороший sni")."""

    node = await db.get(Node, node_id)
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="node not found")

    existing = (
        await db.execute(select(Inbound).where(Inbound.node_id == node.id).limit(1))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="node already has an inbound")

    inbound = await provision_default_inbound(node)
    db.add(inbound)
    node.status = NodeStatus.active
    node.consecutive_failures = 0
    await db.commit()
    await db.refresh(inbound)
    return inbound


@router.post("/{node_id}/inbound/rotate-sni", response_model=InboundOut)
async def rotate_sni(node_id: str, db: AsyncSession = Depends(get_db)) -> Inbound:
    """Re-probes for a working SNI and swaps the inbound's REALITY dest in
    place (README: admin can trigger this from the bot when a node's current
    SNI gets blocked). Existing clients on the inbound are kept, but every
    VLESS URI already handed out for it stops working -- see
    node_provisioner.rotate_inbound_sni for why that's unavoidable."""

    node = await db.get(Node, node_id)
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="node not found")

    inbound = (
        await db.execute(select(Inbound).where(Inbound.node_id == node.id).limit(1))
    ).scalar_one_or_none()
    if inbound is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="node has no inbound to rotate")

    inbound.sni = await rotate_inbound_sni(node, inbound)
    await db.commit()
    await db.refresh(inbound)
    return inbound
