from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_internal_api_key
from app.core.security import encrypt_secret
from app.db.models import Node
from app.db.session import get_db
from app.schemas.nodes import NodeCreate, NodeCredentialsUpdate, NodeOut

router = APIRouter(prefix="/nodes", tags=["nodes"], dependencies=[Depends(require_internal_api_key)])


@router.post("", response_model=NodeOut, status_code=status.HTTP_201_CREATED)
async def create_node(payload: NodeCreate, db: AsyncSession = Depends(get_db)) -> Node:
    node = Node(
        name=payload.name,
        ip=payload.ip,
        panel_base_url=payload.panel_base_url,
        panel_login=payload.panel_login,
        panel_password_encrypted=encrypt_secret(payload.panel_password),
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
