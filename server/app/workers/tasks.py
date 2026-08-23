"""Background jobs for node bootstrap and health checks."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.core.security import decrypt_secret, encrypt_secret

from app.db.models import (
    Alert,
    AlertStatus,
    Inbound,
    Node,
    NodeStatus,
)

from app.db.session import async_session_maker

from app.services.node_bootstrap import (
    bootstrap_node,
)

from app.services.node_provisioner import (
    provision_default_inbound,
)

from app.services.settings_store import (
    get_setting,
)

from app.services.telegram_notifier import (
    notify_admins,
)

from app.services.threexui_client import (
    get_pooled_client,
)


async def health_check_nodes(ctx) -> None:

    async with async_session_maker() as db:

        threshold = int(
            await get_setting(
                db,
                "node_alert_consecutive_failure_threshold",
            )
        )

        # `installing`/`provisioning` nodes are mid-setup (bootstrap_node_job
        # or a manual provision_inbound owns their status) and typically have
        # no inbound yet -- skip them here so a panel-reachable-but-not-yet-
        # provisioned node never gets promoted straight to `active` and
        # picked up by the balancer before it can actually serve a client. An
        # `installing` node in particular usually has no panel to reach yet
        # at all; probing it here would just add a spurious consecutive
        # failure for something bootstrap_node_job is already handling.
        nodes = (
            await db.execute(
                select(Node).where(
                    Node.status.notin_(
                        [NodeStatus.installing, NodeStatus.provisioning]
                    )
                )
            )
        ).scalars().all()

        with_inbound = set(
            (
                await db.execute(
                    select(
                        Inbound.node_id
                    ).distinct()
                )
            ).scalars()
        )

        new_alerts: list[Alert] = []

        for node in nodes:

            client = get_pooled_client(node)

            try:

                await client.health()

            except Exception as exc:

                node.consecutive_failures += 1

                if (
                    node.consecutive_failures
                    >= threshold
                    and node.status
                    != NodeStatus.unstable
                ):

                    node.status = NodeStatus.unstable

                    existing = (
                        await db.execute(
                            select(Alert).where(
                                Alert.node_id == node.id,
                                Alert.status
                                == AlertStatus.open,
                            )
                        )
                    ).scalar_one_or_none()

                    if existing is None:

                        alert = Alert(
                            node_id=node.id,
                            alert_type="node_unstable",
                            message=(
                                f"{node.consecutive_failures} "
                                f"consecutive health-check failures: "
                                f"{exc}"
                            ),
                        )

                        db.add(alert)
                        new_alerts.append(alert)

            else:

                node.consecutive_failures = 0

                if (
                    node.id in with_inbound
                    and node.status
                    != NodeStatus.active
                ):

                    node.status = NodeStatus.active

                    open_alerts = (
                        await db.execute(
                            select(Alert).where(
                                Alert.node_id == node.id,
                                Alert.status
                                == AlertStatus.open,
                            )
                        )
                    ).scalars().all()

                    for alert in open_alerts:

                        alert.status = (
                            AlertStatus.resolved
                        )

                        alert.resolved_at = (
                            datetime.now(timezone.utc)
                        )

        await db.commit()

        for alert in new_alerts:

            await notify_admins(
                f"⚠️ Нода {alert.node_id} "
                f"нестабильна:\n"
                f"{alert.message}"
            )


async def bootstrap_node_job(
    ctx,
    node_id: str,
    ssh_user: str,
    ssh_port: int,
    ssh_password: str | None,
    ssh_private_key: str | None,
) -> None:

    async with async_session_maker() as db:

        node = await db.get(
            Node,
            node_id,
        )

        if node is None:
            return

        try:

            # ----------------------------------------------------
            # SSH bootstrap
            # ----------------------------------------------------

            bootstrap_result = (
                await bootstrap_node(
                    ssh_host=node.ip,
                    panel_login=node.panel_login,
                    panel_password=decrypt_secret(
                        node.panel_password_encrypted
                    ),
                    panel_port=2053,
                    ssh_user=ssh_user,
                    ssh_port=ssh_port,
                    ssh_password=ssh_password,
                    ssh_private_key=ssh_private_key,
                )
            )

            # Save the actual URL returned by the bootstrap.
            node.panel_base_url = (
                bootstrap_result.panel_base_url
            )

            # The API token is minted during install and printed exactly
            # once; if we don't store it now it is unrecoverable. With it,
            # panel calls authenticate via a Bearer header and skip the
            # login/CSRF round trip entirely.
            if bootstrap_result.panel_api_token:
                node.panel_api_token_encrypted = encrypt_secret(
                    bootstrap_result.panel_api_token
                )

            # ----------------------------------------------------
            # Verify 3x-ui from the main server
            # ----------------------------------------------------

            client = get_pooled_client(node)

            await client.list_inbounds()

            # ----------------------------------------------------
            # Create REALITY inbound
            # ----------------------------------------------------

            inbound = (
                await provision_default_inbound(
                    node,
                    sni=bootstrap_result.sni,
                    # We just installed 3x-ui on this box and reset its
                    # panel credentials; anything left on tcp/443 is ours
                    # to replace -- usually our own leftover from an
                    # earlier bootstrap of the same machine.
                    takeover=True,
                )
            )

            db.add(inbound)

            # Node.sni is used by the bot/API.
            node.sni = inbound.sni

            node.status = NodeStatus.active
            node.consecutive_failures = 0

            await db.commit()

        except Exception as exc:

            await db.rollback()

            node = await db.get(
                Node,
                node_id,
            )

            if node is not None:

                node.status = NodeStatus.unstable

                db.add(
                    Alert(
                        node_id=node.id,
                        alert_type="bootstrap_failed",
                        # Roomier than the Telegram alert: a bootstrap
                        # failure carries the node's own systemd state and
                        # x-ui log, and the alert row is where that is still
                        # readable after the message has been trimmed.
                        message=str(exc)[:6000],
                    )
                )

                await db.commit()

            await notify_admins(
                f"❌ Нода {node_id} "
                f"не установилась:\n\n"
                f"{exc}"
            )

            return

        await notify_admins(
            f"✅ Нода «{node.name}» "
            f"({node.ip}) полностью готова.\n\n"
            f"3x-ui: {node.panel_base_url}\n"
            f"SNI: {inbound.sni}\n"
            f"VPN: VLESS + REALITY / TCP / 443"
        )