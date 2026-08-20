"""Picks which node a new client should be added to.

This is intentionally a placeholder for Etap 1/2 (see PLAN.md): the real
implementation needs actual per-node load (client count, traffic, CPU) rather
than a naive count, plus circuit-breaking around nodes that are `unstable`.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Client, Inbound, Node, NodeStatus


async def pick_least_loaded_node(db: AsyncSession) -> Node | None:
    client_count = (
        select(Inbound.node_id, func.count(Client.id).label("client_count"))
        .join(Client, Client.inbound_id == Inbound.id, isouter=True)
        .group_by(Inbound.node_id)
        .subquery()
    )

    stmt = (
        select(Node)
        .outerjoin(client_count, client_count.c.node_id == Node.id)
        .where(Node.status == NodeStatus.active)
        .order_by(func.coalesce(client_count.c.client_count, 0).asc())
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()
