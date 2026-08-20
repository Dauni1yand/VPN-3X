from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_internal_api_key
from app.db.models import AdView, Payment, PaymentStatus
from app.db.session import get_db
from app.schemas.clients import ClientOut
from app.schemas.subscriptions import AdViewGrant, PaymentConfirm
from app.services.client_issuer import issue_client
from app.services.rate_limit import enforce_rate_limit
from app.services.settings_store import get_setting
from app.services.users import get_or_create_user

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"], dependencies=[Depends(require_internal_api_key)])

_AD_DURATION_SETTING_KEY = {"short": "ad_short_duration_seconds", "long": "ad_long_duration_seconds"}

# Loose enough for legitimate repeated ad-watching (a 15-min ad every 15
# min is 4/hour), tight enough to bound a script spamming fake impression
# IDs -- defense in depth on top of Cloudflare's edge rate limiting.
AD_VIEW_RATE_LIMIT = 20
AD_VIEW_RATE_WINDOW_SECONDS = 60 * 60


@router.post("/ad-view", response_model=ClientOut, status_code=status.HTTP_201_CREATED)
async def grant_ad_view(
    payload: AdViewGrant,
    db: AsyncSession = Depends(get_db),
    cf_ip_country: str | None = Header(default=None, alias="CF-IPCountry"),
) -> ClientOut:
    await enforce_rate_limit(
        f"ad-view:{payload.user_telegram_id}", limit=AD_VIEW_RATE_LIMIT, window_seconds=AD_VIEW_RATE_WINDOW_SECONDS
    )
    existing = (
        await db.execute(
            select(AdView).where(AdView.provider_impression_id == payload.provider_impression_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="ad impression already credited")

    duration = int(await get_setting(db, _AD_DURATION_SETTING_KEY[payload.ad_type.value]))

    client = await issue_client(
        db,
        payload.user_telegram_id,
        duration,
        client_country=cf_ip_country,
        client_latencies=payload.client_latencies,
    )

    user = await get_or_create_user(db, payload.user_telegram_id)
    db.add(
        AdView(
            user_id=user.id,
            ad_type=payload.ad_type,
            granted_seconds=duration,
            provider_impression_id=payload.provider_impression_id,
        )
    )
    try:
        await db.commit()
    except IntegrityError:
        # Two concurrent requests for the same impression both passed the
        # check above before either committed -- the loser's AdView insert
        # collides with the unique constraint instead of granting free time.
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="ad impression already credited")
    return client


@router.post("/payments/confirm", response_model=ClientOut, status_code=status.HTTP_201_CREATED)
async def confirm_payment(payload: PaymentConfirm, db: AsyncSession = Depends(get_db)) -> ClientOut:
    """Called by the Telegram bot once a CryptoBot invoice is paid. Idempotent
    on `provider_invoice_id` so a retried bot call (or a duplicate CryptoBot
    webhook) never issues a second client for the same payment.

    No CF-IPCountry here on purpose: this call comes from the bot's own
    container, not the end user's device, so that header would describe the
    bot's network, not the user's. Only explicit `client_latencies` (if the
    bot ever forwards some from the app) is a valid signal for this path."""

    existing = (
        await db.execute(select(Payment).where(Payment.provider_invoice_id == payload.provider_invoice_id))
    ).scalar_one_or_none()
    if existing is not None and existing.status == PaymentStatus.paid:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="payment already processed")

    user = await get_or_create_user(db, payload.user_telegram_id)

    duration = int(await get_setting(db, "subscription_duration_seconds"))
    client = await issue_client(
        db,
        payload.user_telegram_id,
        duration,
        client_latencies=payload.client_latencies,
    )

    if existing is None:
        db.add(
            Payment(
                user_id=user.id,
                provider="cryptobot",
                provider_invoice_id=payload.provider_invoice_id,
                amount=payload.amount,
                currency=payload.currency,
                status=PaymentStatus.paid,
            )
        )
    else:
        existing.status = PaymentStatus.paid
    try:
        await db.commit()
    except IntegrityError:
        # Same race as grant_ad_view above: a concurrent duplicate for the
        # same invoice_id (double-tap on "I paid" racing the webhook, say)
        # collides on the unique constraint instead of issuing a second
        # client for one payment.
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="payment already processed")
    return client
