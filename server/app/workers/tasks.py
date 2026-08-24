"""Background jobs for node bootstrap and health checks."""

from __future__ import annotations

import asyncio
import logging

from datetime import datetime, timezone

from sqlalchemy import select, update

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

from app.services.remnawave_bootstrap import (
    bootstrap_remnawave_node,
)

from app.services.remnawave_client import (
    get_remnawave,
)

from app.services.remnawave_provisioner import (
    provision_node_profile,
    teardown_node_profile,
)

from app.services.settings_store import (
    get_setting,
)

from app.services.telegram_notifier import (
    notify_admins,
)

logger = logging.getLogger(__name__)

# How long to wait for the panel to report a freshly registered node as
# connected. The panel dials the node, so this covers the node's image
# pull settling plus one handshake -- generous, because failing here marks
# the node unstable and tears down what we just built.
NODE_CONNECT_TIMEOUT_SECONDS = 120


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

        # One call, not one per node. The panel already tracks every node's
        # connection state -- it is the side that dials them -- so asking it
        # once beats probing N boxes ourselves, and it reports the state the
        # panel will actually act on rather than our guess at it.
        try:
            panel_nodes = {
                str(entry.get("uuid")): entry for entry in await get_remnawave().list_nodes()
            }
            panel_error: str | None = None
        except Exception as exc:  # noqa: BLE001 -- report against every node below
            panel_nodes, panel_error = {}, f"{type(exc).__name__}: {exc}"

        for node in nodes:

            try:
                if panel_error is not None:
                    raise RuntimeError(f"панель Remnawave недоступна: {panel_error}")

                state = panel_nodes.get(node.remnawave_node_uuid or "")

                if state is None:
                    raise RuntimeError(
                        "ноды нет в панели Remnawave — её конфиги никуда не ведут"
                    )

                if state.get("isDisabled"):
                    raise RuntimeError("нода отключена в панели")

                if not state.get("isConnected"):
                    raise RuntimeError(
                        "панель не может достучаться до ноды"
                        + (
                            f": {state['lastStatusMessage']}"
                            if state.get("lastStatusMessage")
                            else ""
                        )
                    )

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
    """Bare Ubuntu VPS -> a node the panel is serving traffic through.

    Four steps, in an order that matters:

      1. get the panel's SECRET_KEY, before touching the VPS -- a
         misconfigured panel should fail here, not after we have installed
         Docker on someone's server;
      2. install and start remnawave-node over SSH, and wait for it to
         listen;
      3. build the node's config profile and its own internal squad;
      4. register the node with the panel and wait for it to connect.

    The node is only marked active once the panel says isConnected. A node
    that is registered but not connected serves nothing, and calling it
    active would put it in the balancer's rotation and hand users configs
    for a dead endpoint.
    """

    async with async_session_maker() as db:

        node = await db.get(Node, node_id)

        if node is None:
            return

        panel = None

        try:
            panel = get_remnawave()

            # Fail before we touch the VPS if the panel is unreachable or
            # the token is wrong.
            secret_key = await panel.get_pubkey()

            bootstrap_result = await bootstrap_remnawave_node(
                ssh_host=node.ip,
                secret_key=secret_key,
                ssh_user=ssh_user,
                ssh_port=ssh_port,
                ssh_password=ssh_password,
                ssh_private_key=ssh_private_key,
            )

            inbound = await provision_node_profile(node, sni=bootstrap_result.sni)

            created = await panel.create_node(
                name=_panel_node_name(node),
                address=node.ip,
                port=bootstrap_result.node_port,
                config_profile_uuid=node.config_profile_uuid,
                active_inbounds=[inbound.remnawave_inbound_uuid],
                country_code=node.country,
            )
            node.remnawave_node_uuid = str(created["uuid"])

            connected = await _wait_for_node_connection(panel, node.remnawave_node_uuid)

            db.add(inbound)
            node.sni = inbound.sni
            node.status = NodeStatus.active
            node.consecutive_failures = 0

            retired = await _retire_superseded_nodes(db, node)

            await db.commit()

        except Exception as exc:

            await db.rollback()

            node = await db.get(Node, node_id)

            if node is not None:
                node.status = NodeStatus.unstable
                db.add(
                    Alert(
                        node_id=node.id,
                        alert_type="bootstrap_failed",
                        # Roomier than the Telegram alert: a bootstrap
                        # failure carries the node's container logs, and the
                        # alert row is where that is still readable after
                        # the message has been trimmed.
                        message=str(exc)[:6000],
                    )
                )

                # Whatever we created in the panel before failing would
                # otherwise be orphaned: a profile and squad with no node,
                # which the admin has to find and delete by hand before the
                # same VPS can be bootstrapped again.
                if panel is not None:
                    await _rollback_panel_objects(panel, node)

                await db.commit()

            await notify_admins(f"❌ Нода {node_id} не установилась:\n\n{exc}")

            return

        retired_line = (
            f"\n\n♻️ Заменила {len(retired)} прежнюю ноду на этом IP — "
            "её конфиги больше не работают, выдайте пользователям новые."
            if retired
            else ""
        )

        # Worth stating rather than implying: a registered-but-not-connected
        # node is not serving anything, and the admin needs to know that
        # before they wonder why configs on it do not work.
        connection_line = (
            "статус в панели: подключена"
            if connected
            else "⚠️ статус в панели: ещё не подключилась — проверьте, что порт "
            f"{bootstrap_result.node_port} на ноде открыт для адреса панели"
        )

        await notify_admins(
            f"✅ Нода «{node.name}» ({node.ip}) готова.\n\n"
            f"{connection_line}\n"
            f"SNI: {inbound.sni}\n"
            f"VPN: VLESS + REALITY / TCP / 443"
            f"{retired_line}"
        )


def _panel_node_name(node: Node) -> str:
    """A name the panel will accept: 3-30 characters.

    Node names are admin-entered free text and can be anything, so fall
    back to the id rather than letting the panel reject a whole
    registration over a name.
    """

    name = (node.name or "").strip()
    if 3 <= len(name) <= 30:
        return name
    return f"vpn3x-{node.id.replace('-', '')[:16]}"


async def _wait_for_node_connection(panel, node_uuid: str) -> bool:
    """Polls until the panel reports the node connected.

    The panel dials the node, not the other way round, so this is where a
    firewall that blocks NODE_PORT shows up -- and it shows up as a node
    that stays unconnected rather than as an error anyone would see.
    """

    loop = asyncio.get_running_loop()
    deadline = loop.time() + NODE_CONNECT_TIMEOUT_SECONDS

    while True:
        try:
            state = await panel.get_node(node_uuid)
        except Exception:  # noqa: BLE001 -- keep polling, report at the end
            state = {}

        if state.get("isConnected"):
            return True

        if loop.time() >= deadline:
            logger.warning(
                "node %s registered but not connected after %ss: %s",
                node_uuid,
                NODE_CONNECT_TIMEOUT_SECONDS,
                state.get("lastStatusMessage"),
            )
            return False

        await asyncio.sleep(5)


async def _rollback_panel_objects(panel, node: Node | None) -> None:
    """Deletes whatever this bootstrap managed to create before it failed."""

    if node is None:
        return

    if node.remnawave_node_uuid:
        try:
            await panel.delete_node(node.remnawave_node_uuid)
        except Exception:  # noqa: BLE001 -- best effort; the alert already fired
            pass
        node.remnawave_node_uuid = None

    try:
        await teardown_node_profile(node)
    except Exception:  # noqa: BLE001 -- same
        pass

    node.config_profile_uuid = None
    node.internal_squad_uuid = None
