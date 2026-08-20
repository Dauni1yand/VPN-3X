"""Background jobs, run by the arq worker -- deliberately outside the HTTP
request path (see PLAN.md section 6: a slow/unreachable node must never
block API responses to users)."""

from __future__ import annotations

from sqlalchemy import select

from app.core.security import decrypt_secret
from app.db.models import Alert, AlertStatus, Node, NodeStatus, Setting
from app.db.session import async_session_maker
from app.services.threexui_client import ThreeXUIClient

# TODO(Etap 2): read from the `settings` table so an admin can tune this via
# the bot (README: "порог срабатывания можно настроить через админку в тг").
DEFAULT_CONSECUTIVE_FAILURE_ALERT_THRESHOLD = 3


async def _get_alert_threshold(db) -> int:
    setting = await db.get(Setting, "node_alert_consecutive_failure_threshold")
    if setting is None:
        return DEFAULT_CONSECUTIVE_FAILURE_ALERT_THRESHOLD
    return int(setting.value)


async def health_check_nodes(ctx) -> None:
    async with async_session_maker() as db:
        threshold = await _get_alert_threshold(db)
        nodes = (await db.execute(select(Node))).scalars().all()

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
                        db.add(
                            Alert(
                                node_id=node.id,
                                alert_type="node_unstable",
                                message=f"{node.consecutive_failures} consecutive health-check failures: {exc}",
                            )
                        )
            else:
                if node.consecutive_failures > 0 or node.status != NodeStatus.active:
                    node.consecutive_failures = 0
                    node.status = NodeStatus.active
            finally:
                await client.aclose()

        await db.commit()
