from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_internal_api_key
from app.db.session import get_db
from app.schemas.cloudflare import CloudflareConnectRequest
from app.services.audit import log_admin_action
from app.services.cloudflare import CloudflareError, connect_cloudflare, detect_public_ip
from app.services.settings_store import get_setting

router = APIRouter(prefix="/cloudflare", tags=["cloudflare"], dependencies=[Depends(require_internal_api_key)])


@router.post("/connect")
async def cloudflare_connect(payload: CloudflareConnectRequest, db: AsyncSession = Depends(get_db)) -> dict:
    """Points `record_name` at the main server through Cloudflare (proxied
    A record + SSL mode), so this can be triggered from inside the bot
    (README: admin should be able to hook Cloudflare up later) instead of
    needing shell access to the box. Idempotent -- updates the existing
    record if one's already there instead of duplicating it."""

    api_token = await get_setting(db, "cloudflare_api_token")
    zone_id = await get_setting(db, "cloudflare_zone_id")
    if not api_token or not zone_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cloudflare isn't configured yet -- set /setcloudflaretoken and /setcloudflarezone first",
        )

    try:
        server_ip = payload.server_ip or await detect_public_ip()
        result = await connect_cloudflare(api_token, zone_id, payload.record_name, server_ip)
    except CloudflareError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    log_admin_action(
        db,
        admin_telegram_id=payload.admin_telegram_id,
        action="cloudflare_connect",
        target=payload.record_name,
        details=server_ip,
    )
    await db.commit()
    return result
