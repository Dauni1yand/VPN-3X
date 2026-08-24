"""Compares what a node actually serves against what we recorded for it.

REALITY makes this necessary, and the reasoning did not change when the
panel did. A server that refuses a handshake -- wrong shortId, an SNI
outside serverNames, a client core below minClientVer -- does not answer
with an error. It proxies the connection on to `dest`, so the client
completes a TLS handshake, reports the server reachable, shows a latency,
and carries no traffic. Every failure in that family presents identically
from the client side, and identically to a working config.

So the question worth answering is never "is the node up" but "does the
node serve exactly the config we handed out". Under Remnawave that splits
three ways, and all three have to hold:

  1. the panel can reach the node at all (it dials the node, not the
     reverse, so this is the panel's view and not a guess),
  2. the config profile the node is bound to still carries the inbound our
     configs were issued against, with the same REALITY parameters, and
  3. each user is still in the squad that scopes them to this node.

A node can pass any two of those and serve nothing.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Client, ClientStatus, Inbound, Node
from app.services.reality_inbound import MIN_CLIENT_VERSION
from app.services.remnawave_client import get_remnawave
from app.services.remnawave_provisioner import inbound_tag


def _reality_of(profile: dict, tag: str) -> tuple[dict, dict] | tuple[None, None]:
    """The inbound with `tag` and its realitySettings, out of a profile."""

    config = profile.get("config")
    if not isinstance(config, dict):
        return None, None

    for entry in config.get("inbounds") or []:
        if isinstance(entry, dict) and entry.get("tag") == tag:
            stream = entry.get("streamSettings") or {}
            return entry, (stream.get("realitySettings") or {})

    return None, None


async def diagnose_node(db: AsyncSession, node: Node) -> dict:
    report: dict[str, Any] = {"node_id": node.id, "name": node.name, "ip": node.ip}
    problems: list[str] = []

    panel = get_remnawave()

    # --- 1. does the panel have this node, and can it reach it -----------
    if not node.remnawave_node_uuid:
        report["panel"] = "не зарегистрирована"
        report["problems"] = [
            "Ноды нет в панели Remnawave — она ничего не обслуживает. "
            "Переустановите её."
        ]
        return report

    try:
        state = await panel.get_node(node.remnawave_node_uuid)
        report["panel"] = "ok"
    except Exception as exc:  # noqa: BLE001 -- this IS the finding
        report["panel"] = f"{type(exc).__name__}: {exc}"
        report["problems"] = ["Панель Remnawave недоступна с главного сервера."]
        return report

    report["node"] = {
        "isConnected": state.get("isConnected"),
        "isDisabled": state.get("isDisabled"),
        "lastStatusMessage": state.get("lastStatusMessage"),
        "xrayVersion": state.get("xrayVersion"),
        "nodeVersion": state.get("nodeVersion"),
        "xrayUptime": state.get("xrayUptime"),
        "usersOnline": state.get("usersOnline"),
    }

    if state.get("isDisabled"):
        problems.append("Нода отключена в панели.")

    if not state.get("isConnected"):
        problems.append(
            "Панель не может достучаться до ноды"
            + (f": {state['lastStatusMessage']}" if state.get("lastStatusMessage") else "")
            + ". Проверьте, что контейнер remnanode запущен и что его NODE_PORT "
            "открыт для адреса панели."
        )

    ours = (
        await db.execute(select(Inbound).where(Inbound.node_id == node.id).limit(1))
    ).scalar_one_or_none()

    if ours is None:
        report["problems"] = problems + ["В базе нет инбаунда для этой ноды."]
        return report

    # --- 2. does the profile still carry our inbound, unchanged ----------
    active = (state.get("configProfile") or {}).get("activeConfigProfileUuid")
    if active and node.config_profile_uuid and str(active) != node.config_profile_uuid:
        problems.append(
            f"Нода привязана к другому config profile ({active}), а конфиги "
            f"выданы под {node.config_profile_uuid}."
        )

    try:
        profile = await panel.get_config_profile(node.config_profile_uuid or str(active))
    except Exception as exc:  # noqa: BLE001 -- report, don't abort
        report["profile"] = f"недоступен: {type(exc).__name__}: {exc}"
        report["problems"] = problems
        return report

    tag = inbound_tag(node)
    entry, reality = _reality_of(profile, tag)

    if entry is None:
        problems.append(
            f"В config profile ноды нет инбаунда {tag}. Конфиги, выданные "
            "для него, мертвы."
        )
        report["problems"] = problems
        return report

    report["inbound"] = {
        "port": entry.get("port"),
        "security": (entry.get("streamSettings") or {}).get("security"),
        "serverNames": reality.get("serverNames"),
        "shortIds": reality.get("shortIds"),
        "minClientVer": reality.get("minClientVer"),
        "dest": reality.get("dest"),
        "hasPublicKey": bool(reality.get("publicKey")),
    }

    if entry.get("port") != ours.port:
        problems.append(
            f"Порт в профиле {entry.get('port')}, а в конфигах выдан {ours.port}."
        )

    server_names = reality.get("serverNames") or []
    if ours.sni and ours.sni not in server_names:
        problems.append(
            f"SNI в конфиге ({ours.sni}) отсутствует в serverNames ноды ({server_names}). "
            "Рукопожатие будет отвергнуто, и клиент уйдёт на dest."
        )

    short_ids = reality.get("shortIds") or []
    if ours.reality_short_id and ours.reality_short_id not in short_ids:
        problems.append(
            f"shortId в конфиге ({ours.reality_short_id}) отсутствует на ноде ({short_ids}). "
            "Рукопожатие будет отвергнуто, и клиент уйдёт на dest."
        )

    min_ver = reality.get("minClientVer")
    if not min_ver:
        problems.append(
            "minClientVer не задан. xray подставляет 26.3.27 и отвергает "
            "клиентов на более старом ядре — то есть почти все приложения."
        )
    elif min_ver != MIN_CLIENT_VERSION:
        problems.append(f"minClientVer на ноде = {min_ver}, ожидался {MIN_CLIENT_VERSION}.")

    # A node that accepts the connection and cannot forward it looks to the
    # client exactly like a refused handshake, so the egress side is worth
    # naming even though the panel owns the config.
    outbounds = (profile.get("config") or {}).get("outbounds") or []
    if not any(
        isinstance(item, dict) and item.get("protocol") == "freedom" for item in outbounds
    ):
        problems.append(
            "В профиле нет freedom-исходящего: трафик клиента некуда "
            "отправлять. Подключение установится, интернета не будет."
        )

    # --- 3. are our clients still scoped to this node --------------------
    our_clients = list(
        (
            await db.execute(
                select(Client).where(
                    Client.inbound_id == ours.id,
                    Client.status == ClientStatus.active,
                )
            )
        ).scalars()
    )

    detached: list[str] = []
    unavailable = 0

    for client in our_clients:
        if not client.remnawave_user_uuid:
            # Issued under 3x-ui; there is no panel user to check.
            unavailable += 1
            continue
        try:
            user = await panel.get_user(client.remnawave_user_uuid)
        except Exception:  # noqa: BLE001 -- absence of an answer is not drift
            unavailable += 1
            continue

        squads = {
            str(item.get("uuid"))
            for item in (user.get("activeInternalSquads") or [])
            if isinstance(item, dict)
        }
        if node.internal_squad_uuid and node.internal_squad_uuid not in squads:
            detached.append(client.email)

    report["clients"] = {
        "ours_active": len(our_clients),
        "detached": len(detached),
        "unchecked": unavailable,
    }

    if detached:
        problems.append(
            f"{len(detached)} из {len(our_clients)} клиентов больше не состоят в "
            f"squad этой ноды ({', '.join(detached[:3])}"
            f"{'...' if len(detached) > 3 else ''}) — их конфиги подключатся и "
            "не будут передавать трафик."
        )

    report["problems"] = problems
    return report
