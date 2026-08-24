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


class AdminClientCreate(BaseModel):
    user_telegram_id: int
    duration_seconds: int
    admin_telegram_id: int
    # Unlike ClientCreate: explicit, since only the admin path is allowed to
    # bypass the balancer and pick a node directly (README: regular users
    # never see/choose a server).
    target_node_id: str


class ClientMigrate(BaseModel):
    admin_telegram_id: int
    # If omitted, the balancer picks the least-loaded active node other than
    # the client's current one.
    target_node_id: str | None = None


class ClientOut(BaseModel):
    id: str
    status: ClientStatus
    expires_at: datetime
    vless_uri: str
    # Remnawave's subscription link for this client. Worth handing out
    # alongside the raw URI rather than instead of it: a subscription keeps
    # working when the node's parameters change, which a pasted vless:// URI
    # cannot, but not every client app takes one. Optional because rows
    # issued under 3x-ui have no subscription behind them.
    subscription_url: str | None = None

    class Config:
        from_attributes = True
