"""Picks which node a new client should be added to.

Runs synchronously in the request path, right before a config is issued --
NOT on the health-check cron (that one, in app/workers/tasks.py, only tracks
node reachability for alerting and runs on its own minute schedule; it does
not decide client placement).

Two things feed the decision:
  - current load: how many active, non-expired clients each node already has.
  - estimated connection quality between the user and each node.

For connection quality we prefer a real measurement over a guess:
  1. If the caller (the Android app, once it does its own latency probing in
     Etap 4) supplies measured RTTs per node, we use those directly.
  2. Otherwise we fall back to a coarse same-country/different-country guess,
     using the client's country as seen by Cloudflare (`CF-IPCountry`, free
     since the main server already sits behind Cloudflare -- see README) and
     each node's `country` field set by the admin. This is deliberately a
     two-bucket estimate, not a fine-grained distance table -- we don't have
     real numbers to justify anything more precise than "same country or
     not".
  3. If neither is available (e.g. a bot-driven request, which never reaches
     the origin from the end user's IP), latency is unknown and load alone
     decides.

The two signals are combined with a single additive score so that a heavily
loaded "close" node can lose to a lightly loaded "far" one, and vice versa --
tunable via the constants below.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Client, ClientStatus, Inbound, Node, NodeStatus

# TODO(Etap 2): move to the `settings` table (same pattern as the node-alert
# threshold in workers/tasks.py) so an admin can tune this without a deploy.
SAME_COUNTRY_LATENCY_MS = 30.0
DIFFERENT_COUNTRY_LATENCY_MS = 150.0
UNKNOWN_LATENCY_MS = (SAME_COUNTRY_LATENCY_MS + DIFFERENT_COUNTRY_LATENCY_MS) / 2
# How much estimated latency (ms) one additional active client is "worth".
# Higher = load matters more relative to connection quality.
LOAD_PENALTY_MS_PER_ACTIVE_CLIENT = 5.0


async def _active_client_counts(db: AsyncSession) -> dict[str, int]:
    now = datetime.now(timezone.utc)
    stmt = (
        select(Inbound.node_id, func.count(Client.id))
        .join(Client, Client.inbound_id == Inbound.id, isouter=True)
        .where((Client.id.is_(None)) | ((Client.status == ClientStatus.active) & (Client.expires_at > now)))
        .group_by(Inbound.node_id)
    )
    return dict((await db.execute(stmt)).all())


def _estimate_latency_ms(
    node: Node, client_country: str | None, client_latencies: dict[str, float] | None
) -> float:
    if client_latencies and node.id in client_latencies:
        return client_latencies[node.id]
    if client_country and node.country:
        return SAME_COUNTRY_LATENCY_MS if client_country == node.country else DIFFERENT_COUNTRY_LATENCY_MS
    return UNKNOWN_LATENCY_MS


async def pick_node_for_client(
    db: AsyncSession,
    *,
    client_country: str | None = None,
    client_latencies: dict[str, float] | None = None,
) -> Node | None:
    nodes = (await db.execute(select(Node).where(Node.status == NodeStatus.active))).scalars().all()
    if not nodes:
        return None

    client_counts = await _active_client_counts(db)

    def score(node: Node) -> float:
        load = client_counts.get(node.id, 0)
        latency = _estimate_latency_ms(node, client_country, client_latencies)
        return latency + LOAD_PENALTY_MS_PER_ACTIVE_CLIENT * load

    return min(nodes, key=score)
