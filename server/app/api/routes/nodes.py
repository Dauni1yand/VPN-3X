from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_internal_api_key
from app.core.config import settings
from app.core.security import encrypt_secret
from app.db.models import Inbound, Node, NodeStatus
from app.db.session import get_db
from app.schemas.inbounds import InboundOut
from app.schemas.nodes import NodeBootstrapRequest, NodeCreate, NodeCredentialsUpdate, NodeOut
from app.services.audit import log_admin_action
from app.services.node_doctor import diagnose_node
from app.services.queue import get_queue
from app.services.remnawave_client import RemnawaveError, get_remnawave
from app.services.remnawave_provisioner import (
    provision_node_profile,
    rotate_profile_sni,
    teardown_node_profile,
)

router = APIRouter(prefix="/nodes", tags=["nodes"], dependencies=[Depends(require_internal_api_key)])


@router.post("", response_model=NodeOut, status_code=status.HTTP_201_CREATED)
async def create_node(payload: NodeCreate, db: AsyncSession = Depends(get_db)) -> Node:
    node = Node(
        name=payload.name,
        ip=payload.ip,
        # Legacy 3x-ui fields, still accepted so a node can be recorded from
        # an existing deployment. A Remnawave node has no panel of its own.
        panel_base_url=payload.panel_base_url,
        panel_login=payload.panel_login,
        panel_password_encrypted=(
            encrypt_secret(payload.panel_password) if payload.panel_password else None
        ),
        country=payload.country.upper() if payload.country else None,
    )
    db.add(node)
    await db.flush()
    log_admin_action(db, admin_telegram_id=payload.admin_telegram_id, action="create_node", target=node.id)
    await db.commit()
    await db.refresh(node)
    return node


@router.get("", response_model=list[NodeOut])
async def list_nodes(db: AsyncSession = Depends(get_db)) -> list[NodeOut]:
    nodes = list((await db.execute(select(Node).order_by(Node.created_at))).scalars())
    # One query for every node's inbound state rather than one per node --
    # the bot renders this list on every "Ноды" tap.
    with_inbound = set((await db.execute(select(Inbound.node_id).distinct())).scalars())
    return [
        NodeOut.model_validate(node).model_copy(update={"has_inbound": node.id in with_inbound})
        for node in nodes
    ]


@router.patch("/{node_id}/credentials", response_model=NodeOut)
async def update_node_credentials(
    node_id: str, payload: NodeCredentialsUpdate, db: AsyncSession = Depends(get_db)
) -> Node:
    node = await db.get(Node, node_id)
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="node not found")

    if payload.panel_login:
        node.panel_login = payload.panel_login
    if payload.panel_password:
        node.panel_password_encrypted = encrypt_secret(payload.panel_password)

    log_admin_action(db, admin_telegram_id=payload.admin_telegram_id, action="update_node_credentials", target=node.id)
    await db.commit()
    await db.refresh(node)
    return node


@router.delete("/{node_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_node(node_id: str, admin_telegram_id: int, db: AsyncSession = Depends(get_db)) -> None:
    node = await db.get(Node, node_id)
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="node not found")
    # Delete in the panel first. Dropping our row while the panel still
    # serves the node leaves an endpoint nothing on this side knows about,
    # and a profile and squad the admin has to hunt down by hand.
    if node.remnawave_node_uuid:
        try:
            await get_remnawave().delete_node(node.remnawave_node_uuid)
        except RemnawaveError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"не удалось удалить ноду в панели: {exc}",
            ) from exc

    try:
        await teardown_node_profile(node)
    except RemnawaveError:
        # Best-effort: the node itself is already gone, and a leftover
        # profile must not block removing our row.
        pass

    log_admin_action(db, admin_telegram_id=admin_telegram_id, action="delete_node", target=node.id)
    await db.delete(node)
    await db.commit()


@router.post("/bootstrap", response_model=NodeOut, status_code=status.HTTP_202_ACCEPTED)
async def bootstrap_node_route(payload: NodeBootstrapRequest, db: AsyncSession = Depends(get_db)) -> NodeOut:
    """Kicks off "bare Ubuntu VPS -> serving VLESS node" and returns
    straight away with the node in `installing`.

    The install itself (apt + the 3x-ui installer over SSH) takes minutes,
    so it runs in the worker rather than in this request: holding the
    request open for that long meant the caller's HTTP client timed out
    before the work finished and the admin was left guessing. The node is
    visible in the list immediately, flips to `active` when the worker
    finishes, and the admin is notified either way.

    Status is `installing`, not the more general `provisioning`, so that
    /nodes/{id}/inbound refuses to touch it while the job still owns it --
    an admin tapping "Создать инбаунд" on a node whose install isn't done
    yet used to race the job and hit the 3x-ui panel before it existed.

    SSH credentials are passed to the job and never persisted. Nothing
    else needs storing: a Remnawave node has no panel of its own to hold
    credentials for -- the one panel this deployment talks to is configured
    once in .env."""

    # Checked before anything is created. This used to surface from inside
    # the worker, which meant the admin typed the whole wizard, waited, and
    # then got a failure notification with a node left behind in `unstable`
    # that they had to delete by hand. Nothing about a missing panel URL
    # needs a node row to discover.
    try:
        get_remnawave()
    except RemnawaveError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"{exc}\n\nГлавный сервер управляет нодами только через панель "
                "Remnawave, без неё установка ноды невозможна. Поднимите панель "
                "(docker compose --profile remnawave up -d), создайте токен в "
                "Settings → API Tokens и впишите REMNAWAVE_BASE_URL и "
                "REMNAWAVE_TOKEN в .env."
            ),
        ) from exc

    node = Node(
        name=payload.name,
        ip=payload.ip,
        country=payload.country.upper() if payload.country else None,
        status=NodeStatus.installing,
    )
    db.add(node)
    await db.flush()

    log_admin_action(db, admin_telegram_id=payload.admin_telegram_id, action="bootstrap_node", target=node.id)
    await db.commit()
    await db.refresh(node)

    try:
        queue = await get_queue()
        await queue.enqueue_job(
            "bootstrap_node_job",
            node.id,
            payload.ssh_user,
            payload.ssh_port,
            payload.ssh_password,
            payload.ssh_private_key,
        )
    except Exception as exc:  # noqa: BLE001 -- queue unreachable, serialization, ...
        # The row is already committed at this point, so without this it
        # would sit in `provisioning` forever with no job to pick it up.
        # Drop it and tell the caller what actually went wrong instead of
        # letting this surface as a bare 500.
        await db.delete(node)
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"не удалось поставить установку в очередь ({type(exc).__name__}: {exc}). "
            "Проверьте, что контейнер redis запущен: docker compose ps",
        ) from exc

    return NodeOut.model_validate(node)


@router.post("/{node_id}/inbound", response_model=InboundOut, status_code=status.HTTP_201_CREATED)
async def provision_inbound(node_id: str, admin_telegram_id: int, db: AsyncSession = Depends(get_db)) -> Inbound:
    """Registers a node that is already running remnawave-node.

    The automatic path (/nodes/bootstrap) installs the container and does
    this itself. This is the manual retry: the box is set up, but the panel
    side -- config profile, internal squad, node registration -- is not.
    """

    node = await db.get(Node, node_id)
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="node not found")
    if node.status == NodeStatus.installing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="нода ещё устанавливается автоматически, подождите завершения",
        )

    existing = (
        await db.execute(select(Inbound).where(Inbound.node_id == node.id).limit(1))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="node already has an inbound")

    panel = get_remnawave()

    try:
        inbound = await provision_node_profile(node)

        created = await panel.create_node(
            name=node.name if 3 <= len(node.name or "") <= 30 else f"vpn3x-{node.id[:12]}",
            address=node.ip,
            port=settings.remnawave_node_port,
            config_profile_uuid=node.config_profile_uuid,
            active_inbounds=[inbound.remnawave_inbound_uuid],
            country_code=node.country,
        )
        node.remnawave_node_uuid = str(created["uuid"])
    except RemnawaveError as exc:
        # Leave nothing half-created in the panel; the admin will retry.
        await teardown_node_profile(node)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    db.add(inbound)
    node.sni = inbound.sni
    node.status = NodeStatus.active
    node.consecutive_failures = 0
    log_admin_action(db, admin_telegram_id=admin_telegram_id, action="provision_inbound", target=node.id)
    await db.commit()
    await db.refresh(inbound)
    return inbound


@router.post("/{node_id}/inbound/rotate-sni", response_model=InboundOut)
async def rotate_sni(node_id: str, admin_telegram_id: int, db: AsyncSession = Depends(get_db)) -> Inbound:
    """Re-probes for a working SNI and swaps the config profile's REALITY
    dest in place (README: admin can trigger this from the bot when a node's
    current SNI gets blocked).

    The keypair and shortId are reused, so only the `sni=` in already-issued
    URIs goes stale -- and clients that hold a subscription rather than a
    pasted URI pick the new one up on their next refresh."""

    node = await db.get(Node, node_id)
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="node not found")

    inbound = (
        await db.execute(select(Inbound).where(Inbound.node_id == node.id).limit(1))
    ).scalar_one_or_none()
    if inbound is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="node has no inbound to rotate")

    try:
        inbound.sni = await rotate_profile_sni(node, inbound)
    except RemnawaveError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    node.sni = inbound.sni
    log_admin_action(db, admin_telegram_id=admin_telegram_id, action="rotate_sni", target=node.id, details=inbound.sni)
    await db.commit()
    await db.refresh(inbound)
    return inbound


@router.get("/{node_id}/xray-log")
async def node_xray_log(node_id: str, count: int = 60, db: AsyncSession = Depends(get_db)) -> dict:
    """The panel's view of the node's Xray.

    Deliberately not what this used to be. 3x-ui exposed the node's raw
    xray-core log over its API, which was the only place a rejected REALITY
    handshake showed up. Remnawave's API has no equivalent -- the logs live
    in the node's own container -- so returning an empty list dressed up as
    logs would be worse than saying so.

    What the panel does know is reported here, and the rest is a shell
    command the admin can run.
    """

    node = await db.get(Node, node_id)
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="node not found")
    if node.status == NodeStatus.installing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="нода ещё устанавливается, статус появится после установки",
        )
    if not node.remnawave_node_uuid:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="нода не зарегистрирована в панели",
        )

    try:
        state = await get_remnawave().get_node(node.remnawave_node_uuid)
    except RemnawaveError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"не удалось получить статус ноды ({exc})",
        ) from exc

    return {
        "node_id": node_id,
        "status": {
            "isConnected": state.get("isConnected"),
            "isDisabled": state.get("isDisabled"),
            "lastStatusMessage": state.get("lastStatusMessage"),
            "lastStatusChange": state.get("lastStatusChange"),
            "xrayVersion": state.get("xrayVersion"),
            "nodeVersion": state.get("nodeVersion"),
            "xrayUptime": state.get("xrayUptime"),
            "usersOnline": state.get("usersOnline"),
        },
        "raw_logs_hint": (
            "Панель Remnawave не отдаёт логи xray через API. "
            "На самой ноде: cd /opt/remnanode && docker compose logs --tail 200"
        ),
    }


@router.get("/{node_id}/diagnose")
async def diagnose(node_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    """Diffs what the node actually serves against what we handed out.

    See node_doctor: every REALITY rejection looks the same from a client --
    a completed handshake, a latency reading, no traffic -- so the useful
    question is whether the node's inbound still matches the configs in
    circulation, not whether the node is reachable.
    """

    node = await db.get(Node, node_id)
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="node not found")
    if node.status == NodeStatus.installing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="нода ещё устанавливается, диагностика будет доступна после установки",
        )

    return await diagnose_node(db, node)
