from pydantic import BaseModel

from app.db.models import AdType


class AdViewGrant(BaseModel):
    user_telegram_id: int
    ad_type: AdType
    provider_impression_id: str


class PaymentConfirm(BaseModel):
    user_telegram_id: int
    provider_invoice_id: str
    plan_code: str
    amount: str
    currency: str
