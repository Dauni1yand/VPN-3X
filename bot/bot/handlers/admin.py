"""Admin-only commands: node management + tunable settings. Gated by
`settings.admin_ids` (README, Работа тг-бота -> Вариант админа)."""

from __future__ import annotations

import httpx
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.config import settings
from bot.services.api_client import server_api

router = Router(name="admin")
router.message.filter(lambda message: message.from_user.id in settings.admin_ids)


@router.message(Command("nodes"))
async def cmd_list_nodes(message: Message) -> None:
    nodes = await server_api.list_nodes()
    if not nodes:
        await message.answer("Нод пока нет.")
        return
    lines = [f"{n['name']} ({n['ip']}) -- {n['status']}" for n in nodes]
    await message.answer("\n".join(lines))


@router.message(Command("addnode"))
async def cmd_add_node(message: Message) -> None:
    # Usage: /addnode <name> <ip> <panel_base_url> <login> <password> [country]
    # country is an ISO-3166 alpha-2 code (e.g. NL) used by the balancer as a
    # coarse latency proxy -- see node_balancer.py.
    parts = (message.text or "").split(maxsplit=6)[1:]
    if len(parts) not in (5, 6):
        await message.answer(
            "Использование: /addnode <name> <ip> <panel_base_url> <login> <password> [country]"
        )
        return

    name, ip, panel_base_url, login, password, *rest = parts
    country = rest[0] if rest else None
    node = await server_api.add_node(name, ip, panel_base_url, login, password, country, message.from_user.id)
    await message.answer(f"Нода добавлена: {node['id']}")


@router.message(Command("delnode"))
async def cmd_delete_node(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Использование: /delnode <node_id>")
        return
    await server_api.delete_node(parts[1], message.from_user.id)
    await message.answer("Нода удалена.")


@router.message(Command("bootstrap"))
async def cmd_bootstrap(message: Message) -> None:
    # Usage: /bootstrap <name> <ip> <ssh_root_password> [country]
    # Goes all the way from a bare VPS to a serving node: installs 3x-ui
    # over SSH, sets our own panel credentials, creates the REALITY inbound.
    parts = (message.text or "").split(maxsplit=4)[1:]
    # Delete the command message first regardless of outcome -- it contains
    # the SSH root password in plaintext, and a private Telegram chat lets a
    # bot delete the user's own message (unlike groups, no admin rights
    # needed), so don't leave it sitting in the chat history.
    try:
        await message.delete()
    except Exception:  # noqa: BLE001 -- best-effort scrub, never block on it
        pass

    if len(parts) not in (3, 4):
        await message.answer("Использование: /bootstrap <name> <ip> <ssh_root_password> [country]")
        return

    name, ip, ssh_password, *rest = parts
    country = rest[0] if rest else None
    status_msg = await message.answer("Устанавливаю 3x-ui и настраиваю ноду, это займёт пару минут...")
    node = await server_api.bootstrap_node(name, ip, ssh_password, country, message.from_user.id)
    await status_msg.edit_text(f"Нода готова и раздаёт VPN: {node['id']} ({node['status']})")


@router.message(Command("provision"))
async def cmd_provision(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Использование: /provision <node_id>")
        return
    inbound = await server_api.provision_inbound(parts[1], message.from_user.id)
    await message.answer(
        f"Инбаунд создан: порт {inbound['port']}, sni={inbound['sni']}"
    )


@router.message(Command("rotatesni"))
async def cmd_rotate_sni(message: Message) -> None:
    # README (тг-бот, вариант админа): "изменять параметры нод (смена сни,
    # админ нажимает кнопку и главный сервер проверяет рабочие варианты сни
    # для конкретной ноды и устанавливает его)".
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Использование: /rotatesni <node_id>")
        return
    inbound = await server_api.rotate_sni(parts[1], message.from_user.id)
    await message.answer(
        f"Новый sni: {inbound['sni']}. Ранее выданные конфиги для этой ноды перестанут работать."
    )


@router.message(Command("issueconfig"))
async def cmd_issue_admin_config(message: Message) -> None:
    # Usage: /issueconfig <telegram_id> <hours> <node_id> -- unlike a regular
    # user, the admin explicitly picks which node to issue on (README:
    # "выдача админских VPN-конфигов с выбором сервера"), e.g. to test one.
    parts = (message.text or "").split(maxsplit=3)[1:]
    if len(parts) != 3 or not parts[0].lstrip("-").isdigit() or not parts[1].isdigit():
        await message.answer("Использование: /issueconfig <telegram_id> <hours> <node_id>")
        return
    telegram_id, hours, node_id = parts
    result = await server_api.create_admin_client(
        int(telegram_id), int(hours) * 3600, node_id, message.from_user.id
    )
    await message.answer(f"Конфиг выдан:\n`{result['vless_uri']}`", parse_mode="Markdown")


@router.message(Command("migrate"))
async def cmd_migrate_client(message: Message) -> None:
    # Usage: /migrate <client_id> [target_node_id] -- moves a client to
    # another node without cutting its remaining time short (README:
    # "переброс пользователя с одной ноды на другую"). Without
    # target_node_id, the balancer picks the least-loaded other active node.
    parts = (message.text or "").split(maxsplit=2)[1:]
    if len(parts) not in (1, 2):
        await message.answer("Использование: /migrate <client_id> [target_node_id]")
        return
    client_id, *rest = parts
    target_node_id = rest[0] if rest else None
    result = await server_api.migrate_client(client_id, message.from_user.id, target_node_id)
    await message.answer(f"Клиент перенесён. Новый конфиг:\n`{result['vless_uri']}`", parse_mode="Markdown")


@router.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    values = await server_api.list_settings()
    lines = [f"{key} = {value}" for key, value in sorted(values.items())]
    await message.answer(
        "\n".join(lines)
        + "\n\n/setprice <amount> <asset>\n/setaddurations <short_min> <long_min>\n"
        "/setalertthreshold <n>\n/setsubduration <days>\n/setcryptobottoken <token>\n"
        "/setcloudflaretoken <token>\n/setcloudflarezone <zone_id>\n"
        "/connectcloudflare <record_name> [server_ip]"
    )


@router.message(Command("setprice"))
async def cmd_set_price(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=2)[1:]
    if len(parts) != 2:
        await message.answer("Использование: /setprice <amount> <asset>, например /setprice 5 USDT")
        return
    amount, asset = parts
    await server_api.set_setting("subscription_price_amount", amount, message.from_user.id)
    await server_api.set_setting("subscription_price_asset", asset.upper(), message.from_user.id)
    await message.answer(f"Цена подписки: {amount} {asset.upper()}")


@router.message(Command("setaddurations"))
async def cmd_set_ad_durations(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=2)[1:]
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        await message.answer("Использование: /setaddurations <short_min> <long_min>, например /setaddurations 15 60")
        return
    short_min, long_min = parts
    await server_api.set_setting("ad_short_duration_seconds", str(int(short_min) * 60), message.from_user.id)
    await server_api.set_setting("ad_long_duration_seconds", str(int(long_min) * 60), message.from_user.id)
    await message.answer(f"Длительности за рекламу: {short_min} мин / {long_min} мин")


@router.message(Command("setalertthreshold"))
async def cmd_set_alert_threshold(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /setalertthreshold <n>, например /setalertthreshold 3")
        return
    await server_api.set_setting("node_alert_consecutive_failure_threshold", parts[1], message.from_user.id)
    await message.answer(f"Порог алерта: {parts[1]} подряд неудачных health-check'ов")


@router.message(Command("setsubduration"))
async def cmd_set_subscription_duration(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /setsubduration <days>, например /setsubduration 30")
        return
    await server_api.set_setting(
        "subscription_duration_seconds", str(int(parts[1]) * 24 * 60 * 60), message.from_user.id
    )
    await message.answer(f"Длительность подписки: {parts[1]} дней")


@router.message(Command("setcryptobottoken"))
async def cmd_set_cryptobot_token(message: Message) -> None:
    # Usage: /setcryptobottoken <token> -- token comes from @CryptoBot's
    # "/pay" -> "Create App". Stored encrypted in the server's settings
    # table (settings_store.py), not an env var, so it can be added or
    # rotated without a redeploy.
    parts = (message.text or "").split(maxsplit=1)
    # Same reasoning as /bootstrap: this message carries a secret in
    # plaintext, and a bot can delete the user's own message in a private
    # chat -- scrub it regardless of whether the command was well-formed.
    try:
        await message.delete()
    except Exception:  # noqa: BLE001 -- best-effort scrub, never block on it
        pass

    if len(parts) != 2:
        await message.answer("Использование: /setcryptobottoken <token>")
        return
    await server_api.set_setting("cryptobot_api_token", parts[1], message.from_user.id)
    await message.answer("Токен CryptoBot сохранён.")


@router.message(Command("setcloudflaretoken"))
async def cmd_set_cloudflare_token(message: Message) -> None:
    # Usage: /setcloudflaretoken <token> -- a Cloudflare API TOKEN (not the
    # legacy Global API Key), scoped to Zone.DNS:Edit + Zone.Zone
    # Settings:Edit for the zone you'll connect. Create one at
    # https://dash.cloudflare.com/profile/api-tokens
    parts = (message.text or "").split(maxsplit=1)
    try:
        await message.delete()  # carries a secret in plaintext, same as /setcryptobottoken
    except Exception:  # noqa: BLE001 -- best-effort scrub, never block on it
        pass

    if len(parts) != 2:
        await message.answer("Использование: /setcloudflaretoken <token>")
        return
    await server_api.set_setting("cloudflare_api_token", parts[1], message.from_user.id)
    await message.answer("Токен Cloudflare сохранён.")


@router.message(Command("setcloudflarezone"))
async def cmd_set_cloudflare_zone(message: Message) -> None:
    # Usage: /setcloudflarezone <zone_id> -- found on the domain's Overview
    # page in the Cloudflare dashboard. Not a secret on its own (visible to
    # anyone with dashboard access to the zone), so no need to scrub the
    # message the way the API token commands do.
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Использование: /setcloudflarezone <zone_id>")
        return
    await server_api.set_setting("cloudflare_zone_id", parts[1], message.from_user.id)
    await message.answer("Zone ID Cloudflare сохранён.")


@router.message(Command("connectcloudflare"))
async def cmd_connect_cloudflare(message: Message) -> None:
    # Usage: /connectcloudflare <record_name> [server_ip] -- points
    # record_name at this server through Cloudflare (proxied A record +
    # SSL mode). server_ip is auto-detected if omitted. Needs
    # /setcloudflaretoken and /setcloudflarezone to have been run first.
    # README: put the main server behind Cloudflare CDN; this only does the
    # DNS/SSL half -- the optional firewall lock-down needs actual host
    # access and stays a script run on the server itself, see
    # scripts/setup_cloudflare.sh.
    parts = (message.text or "").split(maxsplit=2)[1:]
    if len(parts) not in (1, 2):
        await message.answer("Использование: /connectcloudflare <record_name> [server_ip]")
        return
    record_name, *rest = parts
    server_ip = rest[0] if rest else None

    status_msg = await message.answer("Подключаю Cloudflare...")
    try:
        result = await server_api.connect_cloudflare(record_name, server_ip, message.from_user.id)
    except httpx.HTTPStatusError as exc:
        detail = exc.response.json().get("detail", exc.response.text) if exc.response.text else str(exc)
        await status_msg.edit_text(f"Не получилось: {detail}")
        return

    await status_msg.edit_text(
        f"Готово: {result['record_name']} -> {result['server_ip']} "
        f"(зона {result['zone_name']}, проксировано: {'да' if result['proxied'] else 'нет'}).\n"
        "DNS может обновляться несколько минут."
    )
