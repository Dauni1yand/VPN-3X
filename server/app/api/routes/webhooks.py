"""Public webhook endpoints -- NOT behind require_internal_api_key, since
these are called by external providers (CryptoBot), not our own bot. Each
one authenticates itself differently (here: HMAC signature)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.subscriptions import confirm_payment
from app.db.session import get_db
from app.schemas.subscriptions import PaymentConfirm
from app.services.cryptobot_webhook import verify_webhook_signature
from app.services.rate_limit import enforce_rate_limit

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# This endpoint has no shared-secret gate (require_internal_api_key) since
# CryptoBot, not our own bot, calls it -- rate-limit by IP as a cheap guard
# before even touching the signature check.
CRYPTOBOT_WEBHOOK_RATE_LIMIT = 60
CRYPTOBOT_WEBHOOK_RATE_WINDOW_SECONDS = 60


@router.post("/cryptobot", status_code=status.HTTP_200_OK)
async def cryptobot_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    signature: str = Header(default="", alias="crypto-pay-api-signature"),
) -> dict:
    client_ip = request.client.host if request.client else "unknown"
    await enforce_rate_limit(
        f"cryptobot-webhook:{client_ip}",
        limit=CRYPTOBOT_WEBHOOK_RATE_LIMIT,
        window_seconds=CRYPTOBOT_WEBHOOK_RATE_WINDOW_SECONDS,
    )

    raw_body = await request.body()
    if not verify_webhook_signature(raw_body, signature):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad signature")

    body = await request.json()
    if body.get("update_type") != "invoice_paid":
        return {"ok": True}  # nothing to do for other update types

    invoice = body["payload"]
    # `payload` here is CryptoBot's own passthrough field, set to our
    # "telegram_id:plan_code" string at createInvoice time (see
    # bot/handlers/user.py cmd_subscribe).
    telegram_id_str, plan_code = invoice["payload"].split(":", 1)

    try:
        await confirm_payment(
            PaymentConfirm(
                user_telegram_id=int(telegram_id_str),
                provider_invoice_id=str(invoice["invoice_id"]),
                plan_code=plan_code,
                amount=invoice["amount"],
                currency=invoice["asset"],
            ),
            db,
        )
    except HTTPException as exc:
        if exc.status_code != status.HTTP_409_CONFLICT:
            raise
        # Already processed (e.g. the user's own "I paid" click beat the
        # webhook here, or CryptoBot retried delivery) -- ack with 200 so
        # CryptoBot doesn't keep retrying an event we've already handled.

    return {"ok": True}
