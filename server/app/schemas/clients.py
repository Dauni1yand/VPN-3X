from datetime import datetime

from pydantic import BaseModel

from app.db.models import ClientStatus


class ClientCreate(BaseModel):
    user_telegram_id: int
    duration_seconds: int


class ClientOut(BaseModel):
    id: str
    status: ClientStatus
    expires_at: datetime
    vless_uri: str

    class Config:
        from_attributes = True
