from datetime import datetime

from pydantic import BaseModel


class InboundOut(BaseModel):
    id: str
    node_id: str
    protocol: str
    transport: str
    port: int
    sni: str | None
    reality_public_key: str | None
    reality_short_id: str | None
    created_at: datetime

    class Config:
        from_attributes = True
