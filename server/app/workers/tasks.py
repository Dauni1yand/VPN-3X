"""Background jobs, run by the arq worker -- deliberately outside the HTTP
request path (see PLAN.md section 6: a slow/unreachable node must never
block API responses to users)."""

from __future__ import annotations

from sqlalchemy import select

from app.core.security import decrypt_secret
from app.db.models import Alert, AlertStatus, Node, NodeStatus
from app.db.session import async_session_maker
from app.services.settings_store import get_setting
from app.services.telegram_notifier import notify_admins
from app.services.threexui_client import ThreeXUIClient


async def health_check_nodes(ctx) -> None:
    async with async_session_maker() as db:
        threshold = int(await get_setting(db, "node_alert_consecutive_failure_threshold"))
        nodes = (await db.execute(select(Node))).scalars().all()
        new_alerts: list[Alert] = []

        for node in nodes:
            client = ThreeXUIClient(
                base_url=node.panel_base_url,
                login=node.panel_login,
                password=decrypt_secret(node.panel_password_encrypted),
            )
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
            finally:
                await client.aclose()

        await db.commit()

        # Sent after commit: a Telegram outage must never roll back an
        # already-detected node status change.
        for alert in new_alerts:
            await notify_admins(f"⚠️ Нода {alert.node_id} нестабильна: {alert.message}")
