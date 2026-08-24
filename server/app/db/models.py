import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class NodeStatus(str, enum.Enum):
    # Node row exists but 3x-ui isn't installed/reachable yet -- the
    # background bootstrap_node_job owns it. Manual actions (provision
    # inbound, rotate SNI) must not touch it until the job finishes and
    # flips it to `active` or `unstable` -- doing so races the job and
    # produces a raw connection error (the panel isn't listening yet).
    installing = "installing"
    # Connected via "готовая 3x-ui" (add_node): panel already reachable,
    # but nothing has provisioned an inbound on it yet. This one *is*
    # meant to be acted on manually.
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
    """A VPS serving VPN traffic, as we track it.

    The node itself is owned by the Remnawave panel: it runs the
    remnawave-node container and the panel pushes its Xray config. This row
    is our side of it -- the balancer's inputs (country, status, failure
    count), and the handles needed to address the node in the panel.

    Each node gets its own config profile AND its own internal squad. The
    squad is what preserves a property Remnawave does not have natively:
    the server, not the user, decides which node a config lands on. A user
    placed in exactly one node's squad can only reach that node.
    """

    __tablename__ = "nodes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255))
    ip: Mapped[str] = mapped_column(String(64))
    # --- Remnawave handles ----------------------------------------------
    # Nullable so a row exists from the moment the admin adds the node,
    # before the worker has registered it with the panel -- and so rows
    # predating the migration off 3x-ui still load.
    remnawave_node_uuid: Mapped[str | None] = mapped_column(String(36), nullable=True)
    config_profile_uuid: Mapped[str | None] = mapped_column(String(36), nullable=True)
    internal_squad_uuid: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # --- legacy 3x-ui credentials ----------------------------------------
    # Dead for Remnawave nodes: there is one panel for the deployment now,
    # not one per VPS. Kept nullable rather than dropped so rows created
    # under 3x-ui remain readable, which is what makes the migration
    # reversible.
    panel_base_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    panel_login: Mapped[str | None] = mapped_column(String(255), nullable=True)
    panel_password_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    panel_api_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    """The REALITY inbound a node serves, as we track it.

    In Remnawave the inbound lives inside a config profile's Xray config
    rather than on the node, and is addressed by UUID. We still keep our own
    row for it: the REALITY keypair, shortId, SNI and port are what every
    issued config depends on, and the doctor's whole job is diffing the
    panel's copy against ours.
    """

    __tablename__ = "inbounds"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"))
    # The inbound's UUID inside the config profile, which is how Remnawave
    # addresses it -- in node activeInbounds and in squad membership alike.
    remnawave_inbound_uuid: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Legacy: 3x-ui numbered its inbounds. Nullable so rows created under
    # Remnawave do not have to invent one.
    remote_inbound_id: Mapped[int | None] = mapped_column(nullable=True)
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
    """One user's access to one node, as we track it.

    Backed by a Remnawave *user*. Remnawave models a person, not a per-node
    credential: a user's reach is the union of their squads, and they get
    one subscription covering all of it. We put each user in exactly one
    node's squad, which is what keeps "the server picks the node" true and
    keeps this row meaning what it always meant.
    """

    __tablename__ = "clients"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    inbound_id: Mapped[str] = mapped_column(ForeignKey("inbounds.id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # The VLESS UUID that appears in the share link. Remnawave mints it as
    # the user's vlessUuid; we do not choose it any more.
    remote_client_uuid: Mapped[str] = mapped_column(String(36))
    # The panel's username for this client. Ours are "<telegram_id>-<8 hex>",
    # which fits Remnawave's ^[a-zA-Z0-9_-]{3,36}$ and stays unique.
    email: Mapped[str] = mapped_column(String(255), unique=True)
    # --- Remnawave handles ------------------------------------------------
    remnawave_user_uuid: Mapped[str | None] = mapped_column(String(36), nullable=True)
    remnawave_short_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The panel's subscription link. Handed out alongside the raw config: a
    # subscription keeps working when the node's parameters change, which a
    # pasted vless:// URI cannot.
    subscription_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[ClientStatus] = mapped_column(Enum(ClientStatus, name="client_status"), default=ClientStatus.active)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # The share link exactly as the node's own 3x-ui generated it at issuance
    # (services/vless.py). Stored so "Мой конфиг" hands back the same string
    # we already gave the user without another round trip to the node, and so
    # the doctor can diff it against what the node generates today. Nullable:
    # rows issued before this existed, and any issuance that had to fall back
    # to composing the URI locally, have none.
    vless_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
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
