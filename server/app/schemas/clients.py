from datetime import datetime

from pydantic import BaseModel

from app.db.models import ClientStatus


class ClientCreate(BaseModel):
    user_telegram_id: int
    duration_seconds: int
    # Optional RTT measurements the caller made to candidate nodes, e.g.
    # {node_id: milliseconds}. Takes priority over the country-based guess
    # when present -- see app/services/node_balancer.py.
    client_latencies: dict[str, float] | None = None


class ClientOut(BaseModel):
    id: str
    status: ClientStatus
    expires_at: datetime
    vless_uri: str

    class Config:
        from_attributes = True
