"""Background jobs, run by the arq worker -- deliberately outside the HTTP
request path (see PLAN.md section 6: a slow/unreachable node must never
block API responses to users)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.db.models import Alert, AlertStatus, Node, NodeStatus
from app.db.session import async_session_maker
from app.services.settings_store import get_setting
from app.services.telegram_notifier import notify_admins
from app.services.threexui_client import get_pooled_client


async def health_check_nodes(ctx) -> None:
    async with async_session_maker() as db:
        threshold = int(await get_setting(db, "node_alert_consecutive_failure_threshold"))
        # `provisioning` nodes are mid-setup (bootstrap/provision_inbound own
        # their status) and typically have no inbound yet -- skip them here
        # so a panel-reachable-but-not-yet-provisioned node never gets
        # promoted straight to `active` and picked up by the balancer before
        # it can actually serve a client.
        nodes = (
            await db.execute(select(Node).where(Node.status != NodeStatus.provisioning))
        ).scalars().all()
        new_alerts: list[Alert] = []

        for node in nodes:
            client = get_pooled_client(node)
            try:
                await client.get_online_clients()
            except Exception as exc:  # noqa: BLE001 -- any failure counts as a miss
                node.consecutive_failures += 1
                if node.consecutive_failures >= threshold and node.status != NodeStatus.unstable:
                    node.status = NodeStatus.unstable
                    existing_open_alert = (
                        await db.execute(
                            select(Alert).where(
                                Alert.node_id == node.id, Alert.status == AlertStatus.open
                            )
                        )
                    ).scalar_one_or_none()
                    if existing_open_alert is None:
                        alert = Alert(
                            node_id=node.id,
                            alert_type="node_unstable",
                            message=f"{node.consecutive_failures} consecutive health-check failures: {exc}",
                        )
                        db.add(alert)
                        new_alerts.append(alert)
            else:
                if node.consecutive_failures > 0 or node.status != NodeStatus.active:
                    node.consecutive_failures = 0
                    node.status = NodeStatus.active
                    # Resolve whatever open alert led to this node being
                    # marked unstable -- otherwise the dedup check above
                    # ("existing_open_alert is None") keeps finding this
                    # stale alert forever and silently suppresses every
                    # future real incident on this node.
                    open_alerts = (
                        await db.execute(select(Alert).where(Alert.node_id == node.id, Alert.status == AlertStatus.open))
                    ).scalars().all()
                    for alert in open_alerts:
                        alert.status = AlertStatus.resolved
                        alert.resolved_at = datetime.now(timezone.utc)

        await db.commit()

        # Sent after commit: a Telegram outage must never roll back an
        # already-detected node status change.
        for alert in new_alerts:
            await notify_admins(f"⚠️ Нода {alert.node_id} нестабильна: {alert.message}")
