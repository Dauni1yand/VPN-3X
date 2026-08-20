from pydantic import BaseModel

from app.db.models import AdType


class AdViewGrant(BaseModel):
    user_telegram_id: int
    ad_type: AdType
    provider_impression_id: str
    # See ClientCreate.client_latencies -- same purpose, filled in by the
    # Android app once it does its own latency probing (Etap 4).
    client_latencies: dict[str, float] | None = None


class PaymentConfirm(BaseModel):
    user_telegram_id: int
    provider_invoice_id: str
    plan_code: str
    amount: str
    currency: str
    client_latencies: dict[str, float] | None = None
