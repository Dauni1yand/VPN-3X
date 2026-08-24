"""Background jobs for node bootstrap and health checks."""

from __future__ import annotations

import logging

from datetime import datetime, timezone

from sqlalchemy import select, update

from app.core.security import decrypt_secret, encrypt_secret

from app.db.models import (
    Alert,
    AlertStatus,
    Client,
    ClientStatus,
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

logger = logging.getLogger(__name__)


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


async def _update_xray_to_latest(client, node_id: str) -> str | None:
    """Puts the node on the newest xray-core the panel offers.

    3x-ui ships whatever xray its release bundled, which can be months
    behind; REALITY in particular has changed across 26.x, and a node and a
    client on distant cores negotiate badly.

    Best-effort on purpose. The version list comes from GitHub's API through
    the node, so a rate limit or a slow network there must not fail an
    otherwise healthy bootstrap -- the bundled xray still serves traffic.
    Returns the version installed, or None if it was left alone.
    """

    try:
        versions = await client.list_xray_versions()
    except Exception as exc:  # noqa: BLE001 -- informational, never fatal
        logger.warning("node %s: could not list xray versions: %s", node_id, exc)
        return None

    if not versions:
        logger.warning("node %s: panel offered no xray versions", node_id)
        return None

    # GitHub returns releases newest first and 3x-ui preserves that order.
    latest = versions[0]

    try:
        await client.install_xray(latest)
    except Exception as exc:  # noqa: BLE001 -- keep the node, keep the bundled core
        logger.warning("node %s: xray update to %s failed: %s", node_id, latest, exc)
        return None

    logger.info("node %s: xray updated to %s", node_id, latest)
    return latest


async def _retire_superseded_nodes(db, node) -> list[str]:
    """Retires older Node rows that claim the same IP as `node`.

    One box cannot serve two nodes: bootstrapping replaces its 3x-ui, its
    REALITY keypair and its inbound, so every older row pointing at that IP
    is describing something that no longer exists. Left alone those rows
    stay `active` and keep being chosen -- and their clients keep being
    handed out as working configs, because a REALITY handshake the node no
    longer recognises is answered by proxying to `dest` rather than by an
    error. The user gets a config that connects, pings, and carries no
    traffic.

    Their clients are revoked for the same reason: the keys those configs
    encode are gone, so the config is dead whatever our table says.
    """

    superseded = (
        await db.execute(
            select(Node).where(Node.ip == node.ip, Node.id != node.id)
        )
    ).scalars().all()

    if not superseded:
        return []

    old_ids = [old.id for old in superseded]

    inbound_ids = list(
        (
            await db.execute(select(Inbound.id).where(Inbound.node_id.in_(old_ids)))
        ).scalars()
    )

    if inbound_ids:
        await db.execute(
            update(Client)
            .where(
                Client.inbound_id.in_(inbound_ids),
                Client.status == ClientStatus.active,
            )
            .values(status=ClientStatus.revoked)
        )

    for old in superseded:
        old.status = NodeStatus.disabled

    logger.info(
        "node %s superseded %d older node(s) on %s", node.id, len(old_ids), node.ip
    )

    return old_ids


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
            # Move xray-core to the newest release the panel offers
            # ----------------------------------------------------

            xray_version = await _update_xray_to_latest(client, node_id)

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

            retired = await _retire_superseded_nodes(db, node)

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

        # The xray version is worth reporting either way: it decides which
        # clients can negotiate REALITY at all, so "left as bundled" is a
        # fact the admin wants when a config looks right but won't connect.
        xray_line = (
            f"xray-core: {xray_version}"
            if xray_version
            else "xray-core: не обновился, осталась версия из сборки 3x-ui"
        )

        # Say it plainly: the old configs on that IP are dead, and a user
        # who still holds one gets a connection that pings and carries
        # nothing. They need to be re-issued, not debugged.
        retired_line = (
            f"\n\n♻️ Заменила {len(retired)} прежнюю ноду на этом IP — "
            "её конфиги больше не работают, выдайте пользователям новые."
            if retired
            else ""
        )

        await notify_admins(
            f"✅ Нода «{node.name}» "
            f"({node.ip}) полностью готова.\n\n"
            f"3x-ui: {node.panel_base_url}\n"
            f"{xray_line}\n"
            f"SNI: {inbound.sni}\n"
            f"VPN: VLESS + REALITY / TCP / 443"
            f"{retired_line}"
        )