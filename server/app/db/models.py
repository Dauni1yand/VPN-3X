import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class NodeStatus(str, enum.Enum):
    provisioning = "provisioning"
    active = "active"
    unstable = "unstable"
    disabled = "disabled"


class ClientStatus(str, enum.Enum):
    active = "active"
    expired = "expired"
    revoked = "revoked"


class SubscriptionStatus(str, enum.Enum):
    pending = "pending"
    active = "active"
    expired = "expired"
    cancelled = "cancelled"


class PaymentStatus(str, enum.Enum):
    pending = "pending"
    paid = "paid"
    failed = "failed"
    expired = "expired"


class AlertStatus(str, enum.Enum):
    open = "open"
    resolved = "resolved"


class AdType(str, enum.Enum):
    short = "short"  # e.g. 15 minutes, skippable
    long = "long"  # e.g. 1 hour, non-skippable


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Node(Base):
    """A VPN node running 3x-ui. Panel credentials are stored encrypted;
    the main server logs into the panel (session cookie), it does not rely
    on a per-node API token — see PLAN.md section 4."""

    __tablename__ = "nodes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255))
    ip: Mapped[str] = mapped_column(String(64))
    panel_base_url: Mapped[str] = mapped_column(String(255))
    panel_login: Mapped[str] = mapped_column(String(255))
    panel_password_encrypted: Mapped[str] = mapped_column(Text)
    sni: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # ISO-3166 alpha-2 (e.g. "NL", "DE") set by the admin when the node is
    # added -- used as a coarse proxy for client<->node latency, see
    # node_balancer.py.
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    status: Mapped[NodeStatus] = mapped_column(
        Enum(NodeStatus, name="node_status"), default=NodeStatus.provisioning
    )
    consecutive_failures: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Inbound(Base):
    """Mirrors an inbound created on a node's 3x-ui panel. Clients are added
    to an existing inbound, never as new inbounds (README requirement)."""

    __tablename__ = "inbounds"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"))
    remote_inbound_id: Mapped[int] = mapped_column()  # inbound id as known by 3x-ui
    protocol: Mapped[str] = mapped_column(String(32), default="vless")
    transport: Mapped[str] = mapped_column(String(16), default="tcp")  # tcp (reality) or grpc
    port: Mapped[int] = mapped_column()
    sni: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reality_public_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Encrypted at rest (same scheme as Node.panel_password_encrypted) --
    # needed to rotate the inbound's SNI in place without regenerating the
    # keypair (which would invalidate every already-issued client config's
    # `pbk`, not just the ones affected by the SNI change).
    reality_private_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    reality_short_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("node_id", "remote_inbound_id"),)


class Client(Base):
    """A single VLESS client added to an existing inbound on a node."""

    __tablename__ = "clients"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    inbound_id: Mapped[str] = mapped_column(ForeignKey("inbounds.id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    remote_client_uuid: Mapped[str] = mapped_column(String(36))  # UUID known by 3x-ui
    email: Mapped[str] = mapped_column(String(255), unique=True)  # 3x-ui client identifier
    status: Mapped[ClientStatus] = mapped_column(Enum(ClientStatus, name="client_status"), default=ClientStatus.active)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    plan_code: Mapped[str] = mapped_column(String(64))
    price_amount: Mapped[str] = mapped_column(String(32))
    price_currency: Mapped[str] = mapped_column(String(16))
    status: Mapped[SubscriptionStatus] = mapped_column(
        Enum(SubscriptionStatus, name="subscription_status"), default=SubscriptionStatus.pending
    )
    payment_id: Mapped[str | None] = mapped_column(ForeignKey("payments.id"), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Payment(Base):
    """A CryptoBot (Crypto Pay API) invoice. `provider` is kept generic so an
    alternative PaymentProvider can be added later without a schema change."""

    __tablename__ = "payments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(32), default="cryptobot")
    provider_invoice_id: Mapped[str] = mapped_column(String(128), unique=True)
    amount: Mapped[str] = mapped_column(String(32))
    currency: Mapped[str] = mapped_column(String(16))
    status: Mapped[PaymentStatus] = mapped_column(Enum(PaymentStatus, name="payment_status"), default=PaymentStatus.pending)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AdView(Base):
    """A single rewarded-ad impression that granted VPN time. `provider_impression_id`
    makes crediting idempotent against retries/duplicate callbacks."""

    __tablename__ = "ad_views"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    ad_type: Mapped[AdType] = mapped_column(Enum(AdType, name="ad_type"))
    granted_seconds: Mapped[int] = mapped_column()
    provider_impression_id: Mapped[str] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"))
    alert_type: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    status: Mapped[AlertStatus] = mapped_column(Enum(AlertStatus, name="alert_status"), default=AlertStatus.open)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Setting(Base):
    """Admin-tunable key/value settings: subscription price, ad durations,
    alert thresholds, etc. (README: "по запросу админа менять настройки")."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AdminAuditLog(Base):
    __tablename__ = "admin_audit_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    admin_telegram_id: Mapped[int] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(128))
    target: Mapped[str | None] = mapped_column(String(255), nullable=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
