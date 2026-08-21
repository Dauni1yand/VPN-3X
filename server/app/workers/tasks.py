"""Background jobs, run by the arq worker -- deliberately outside the HTTP
request path (see PLAN.md section 6: a slow/unreachable node must never
block API responses to users)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.core.security import decrypt_secret
from app.db.models import Alert, AlertStatus, Inbound, Node, NodeStatus
from app.db.session import async_session_maker
from app.services.node_bootstrap import PANEL_PORT, bootstrap_node
from app.services.node_provisioner import provision_default_inbound
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
        # A reachable panel is not the same as a usable node: one whose
        # bootstrap failed part-way can answer on its panel port while
        # having no inbound at all. Promoting that to `active` would put it
        # back in the balancer, which then hands users a 503 instead of a
        # config -- so recovery requires an inbound too.
        with_inbound = set((await db.execute(select(Inbound.node_id).distinct())).scalars())
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
                node.consecutive_failures = 0
                if node.id in with_inbound and node.status != NodeStatus.active:
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


async def bootstrap_node_job(
    ctx,
    node_id: str,
    ssh_user: str,
    ssh_port: int,
    ssh_password: str | None,
    ssh_private_key: str | None,
) -> None:
    """Installs 3x-ui on an already-created Node row and provisions its
    inbound, then tells the admin how it went.

    Runs here rather than inside POST /nodes/bootstrap because the SSH work
    takes minutes: holding an HTTP request open that long meant the bot's
    client timed out before the install finished, leaving the admin with
    "сервер не ответил" and no idea whether the node was coming up or not.
    """
    async with async_session_maker() as db:
        node = await db.get(Node, node_id)
        if node is None:
            return  # deleted while queued -- nothing to do

        try:
            await bootstrap_node(
                ssh_host=node.ip,
                panel_login=node.panel_login,
                panel_password=decrypt_secret(node.panel_password_encrypted),
                panel_port=_panel_port(node.panel_base_url),
                ssh_user=ssh_user,
                ssh_port=ssh_port,
                ssh_password=ssh_password,
                ssh_private_key=ssh_private_key,
            )
            inbound = await provision_default_inbound(node)
            db.add(inbound)
            node.status = NodeStatus.active
            node.consecutive_failures = 0
            await db.commit()
        except Exception as exc:  # noqa: BLE001 -- any failure must reach the admin
            await db.rollback()
            node = await db.get(Node, node_id)
            if node is not None:
                node.status = NodeStatus.unstable
                db.add(
                    Alert(
                        node_id=node.id,
                        alert_type="bootstrap_failed",
                        message=str(exc)[:2000],
                    )
                )
                await db.commit()
            await notify_admins(f"❌ Нода {node_id} не установилась:\n\n{exc}")
            return

        await notify_admins(
            f"✅ Нода «{node.name}» ({node.ip}) готова и раздаёт VPN.\nSNI: {inbound.sni}"
        )


def _panel_port(panel_base_url: str) -> int:
    """The port the route already baked into panel_base_url when it created
    the row -- the installer has to configure that same one."""
    tail = panel_base_url.rsplit(":", 1)[-1]
    return int(tail) if tail.isdigit() else PANEL_PORT
