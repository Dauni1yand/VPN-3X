"""Compares what a node actually serves against what we recorded for it.

REALITY makes this necessary. A server that refuses a handshake -- wrong
shortId, an SNI outside serverNames, a client core below minClientVer --
does not answer with an error. It proxies the connection on to `dest`, so
the client completes a TLS handshake, reports the server reachable, shows a
latency, and carries no traffic. Every failure in that family presents
identically from the client side, and identically to a working config.

So the question worth answering is never "is the node up" but "does the
node serve exactly the config we handed out". This reads the inbound back
from the panel and diffs it field by field against our own row.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Client, ClientStatus, Inbound, Node
from app.services.reality_inbound import MIN_CLIENT_VERSION
from app.services.threexui_client import get_pooled_client


def _as_dict(value: Any) -> dict:
    """Inbound.MarshalJSON expands settings/streamSettings into objects, but
    documents a fallback to a JSON string when the stored text isn't valid
    JSON. Both shapes reach us."""

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _same_link(left: str, right: str) -> bool:
    """Whether two share links describe the same connection.

    Compares everything but the `#fragment`: that is the display name, which
    we override on issuance and the panel regenerates from a template
    carrying live traffic and expiry figures, so it differs on every call
    without anything having drifted. Everything before it is stable: the
    panel sorts query parameters, and it picks sni/sid at random only when
    serverNames/shortIds hold more than one entry, which ours never do.
    """

    return left.split("#", 1)[0] == right.split("#", 1)[0]


async def diagnose_node(db: AsyncSession, node: Node) -> dict:
    report: dict[str, Any] = {"node_id": node.id, "name": node.name, "ip": node.ip}
    problems: list[str] = []

    client = get_pooled_client(node)

    # --- panel reachable at all ------------------------------------------
    try:
        remote_inbounds = await client.list_inbounds()
        report["panel"] = "ok"
    except Exception as exc:  # noqa: BLE001 -- this IS the finding
        report["panel"] = f"{type(exc).__name__}: {exc}"
        report["problems"] = ["Панель 3x-ui недоступна с главного сервера."]
        return report

    ours = (
        await db.execute(select(Inbound).where(Inbound.node_id == node.id).limit(1))
    ).scalar_one_or_none()

    if ours is None:
        report["problems"] = ["В базе нет инбаунда для этой ноды."]
        return report

    remote = next(
        (
            candidate
            for candidate in remote_inbounds
            if isinstance(candidate, dict)
            and candidate.get("id") == ours.remote_inbound_id
        ),
        None,
    )

    if remote is None:
        report["problems"] = [
            f"Инбаунд #{ours.remote_inbound_id} есть в нашей базе, "
            f"но его нет на ноде. Конфиги, выданные для него, мертвы."
        ]
        return report

    # --- the fields a client actually negotiates on -----------------------
    stream = _as_dict(remote.get("streamSettings"))
    reality = _as_dict(stream.get("realitySettings"))

    report["inbound"] = {
        "port": remote.get("port"),
        "enabled": remote.get("enable"),
        "security": stream.get("security"),
        "network": stream.get("network"),
        "serverNames": reality.get("serverNames"),
        "shortIds": reality.get("shortIds"),
        "minClientVer": reality.get("minClientVer"),
        "dest": reality.get("dest"),
    }

    if not remote.get("enable", True):
        problems.append("Инбаунд выключен в панели.")

    if remote.get("port") != ours.port:
        problems.append(
            f"Порт на ноде {remote.get('port')}, а в конфигах выдан {ours.port}."
        )

    if stream.get("security") != "reality":
        problems.append(f"security на ноде = {stream.get('security')!r}, а не reality.")

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

    # 3x-ui's own link generator reads pbk from realitySettings.settings.
    # publicKey. Inbounds we created before that field was filled in still
    # have it blank, which makes every link the panel produces for them --
    # its QR code included -- unusable, even though xray itself is fine.
    reality_settings = _as_dict(reality.get("settings"))
    report["inbound"]["publicKeyForLinks"] = bool(reality_settings.get("publicKey"))
    if not reality_settings.get("publicKey"):
        problems.append(
            "В realitySettings.settings.publicKey на ноде пусто: панель 3x-ui "
            "выдаёт ссылки с пустым pbk=. Наши конфиги собираются локально и "
            "работают, но ссылка/QR из самой панели — нет. Лечится "
            "переустановкой инбаунда."
        )

    min_ver = reality.get("minClientVer")
    if not min_ver:
        problems.append(
            "minClientVer не задан. xray подставляет 26.3.27 и отвергает "
            "клиентов на более старом ядре — то есть почти все приложения."
        )
    elif min_ver != MIN_CLIENT_VERSION:
        problems.append(f"minClientVer на ноде = {min_ver}, ожидался {MIN_CLIENT_VERSION}.")

    # --- are our clients present on the node -----------------------------
    settings = _as_dict(remote.get("settings"))
    remote_uuids = {
        str(entry.get("id"))
        for entry in (settings.get("clients") or [])
        if isinstance(entry, dict)
    }

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

    missing = [c for c in our_clients if c.remote_client_uuid not in remote_uuids]

    report["clients"] = {
        "ours_active": len(our_clients),
        "on_node": len(remote_uuids),
        "missing_on_node": len(missing),
    }

    if missing:
        problems.append(
            f"{len(missing)} из {len(our_clients)} выданных клиентов нет на ноде — "
            "их конфиги подключатся и не будут передавать трафик."
        )

    # --- does the node still generate the links we handed out -------------
    #
    # The strongest check available, and the reason issuance asks the node
    # for the link in the first place: the panel generates it from the same
    # inbound row xray is configured from, so regenerating it now and
    # comparing against the string the user actually holds catches every
    # drift class at once -- rotated keys, a changed SNI or shortId, a
    # re-created inbound -- without having to enumerate them.
    stale: list[str] = []
    unavailable = 0

    for ours_client in our_clients:
        if not ours_client.vless_uri:
            continue
        try:
            live = await client.get_client_links(ours_client.email)
        except Exception:  # noqa: BLE001 -- absence of an answer is not drift
            unavailable += 1
            continue
        if not any(_same_link(link, ours_client.vless_uri) for link in live):
            stale.append(ours_client.email)

    report["links"] = {"checked": len(our_clients), "stale": len(stale), "unavailable": unavailable}

    if stale:
        problems.append(
            f"У {len(stale)} клиентов выданная ссылка больше не совпадает с той, "
            f"которую нода генерирует сейчас ({', '.join(stale[:3])}"
            f"{'...' if len(stale) > 3 else ''}). Эти конфиги подключатся и не "
            "будут передавать трафик."
        )

    # --- has anyone actually connected ------------------------------------
    try:
        onlines = await client.get_online_clients()
        report["online_now"] = onlines
    except Exception as exc:  # noqa: BLE001 -- informational
        report["online_now"] = f"недоступно: {type(exc).__name__}: {exc}"

    try:
        report["xray_log"] = (await client.get_xray_logs(30))[-12:]
    except Exception as exc:  # noqa: BLE001 -- informational
        report["xray_log"] = [f"недоступно: {type(exc).__name__}: {exc}"]

    report["problems"] = problems
    return report
