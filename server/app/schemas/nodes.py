from datetime import datetime

from pydantic import BaseModel, Field

from app.db.models import NodeStatus


class NodeCreate(BaseModel):
    name: str
    ip: str
    panel_base_url: str
    panel_login: str
    panel_password: str = Field(repr=False)
    # ISO-3166 alpha-2, e.g. "NL" -- used by the balancer as a coarse latency
    # proxy when the caller has no measured RTT (see node_balancer.py).
    country: str | None = None


class NodeCredentialsUpdate(BaseModel):
    panel_login: str | None = None
    panel_password: str | None = Field(default=None, repr=False)


class NodeOut(BaseModel):
    id: str
    name: str
    ip: str
    panel_base_url: str
    sni: str | None
    country: str | None
    status: NodeStatus
    consecutive_failures: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
