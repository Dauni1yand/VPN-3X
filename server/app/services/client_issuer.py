import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_secret
from app.db.models import Client, ClientStatus, Inbound, User
from app.schemas.clients import ClientOut
from app.services.node_balancer import pick_least_loaded_node
from app.services.threexui_client import ThreeXUIClient
from app.services.vless import build_vless_uri


async def issue_client(db: AsyncSession, telegram_id: int, duration_seconds: int) -> ClientOut:
    """Grants `telegram_id` a VLESS client valid for `duration_seconds`,
    whether that time was earned by watching an ad or by a paid subscription.
    Shared by /clients (direct) and /subscriptions (ad-view / payment)."""

    user = (
        await db.execute(select(User).where(User.telegram_id == telegram_id))
    ).scalar_one_or_none()
    if user is None:
        user = User(telegram_id=telegram_id)
        db.add(user)
        await db.flush()

    node = await pick_least_loaded_node(db)
    if node is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="no active node available")

    inbound = (
        await db.execute(select(Inbound).where(Inbound.node_id == node.id).limit(1))
    ).scalar_one_or_none()
    if inbound is None:
        # Etap 1 (node auto-provisioning) is what's supposed to guarantee this.
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="node has no inbound provisioned")

    client_uuid = str(uuid.uuid4())
    email = f"{telegram_id}-{client_uuid[:8]}"
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)

    threexui = ThreeXUIClient(
        base_url=node.panel_base_url,
        login=node.panel_login,
        password=decrypt_secret(node.panel_password_encrypted),
    )
    try:
        await threexui.add_client(
            inbound_id=inbound.remote_inbound_id,
            client_settings={
                "clients": [
                    {
                        "id": client_uuid,
                        "email": email,
                        "flow": "xtls-rprx-vision",
                        "expiryTime": int(expires_at.timestamp() * 1000),
                    }
                ]
            },
        )
    finally:
        await threexui.aclose()

    client = Client(
        inbound_id=inbound.id,
        user_id=user.id,
        remote_client_uuid=client_uuid,
        email=email,
        status=ClientStatus.active,
        expires_at=expires_at,
    )
    db.add(client)
    await db.commit()
    await db.refresh(client)

    vless_uri = build_vless_uri(node, inbound, client_uuid, remark="vpn-3x")
    return ClientOut(id=client.id, status=client.status, expires_at=client.expires_at, vless_uri=vless_uri)
