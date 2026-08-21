"""Admin panel: nodes, clients, service settings, Cloudflare.

Entirely button-driven. Anything that needs typed input is a short wizard
that asks for one value at a time with a worked example, edits a single
"panel" message in place as it goes, and always offers Отмена -- so an
admin never has to remember a command's argument order.

Gated by `settings.admin_ids` on both messages and callbacks (README,
Работа тг-бота -> Вариант админа)."""

from __future__ import annotations

import html
import ipaddress

import httpx
from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot import keyboards as kb
from bot.config import settings
from bot.menus import safe_edit, show_menu
from bot.services.api_client import server_api
from bot.states import (
    AddNode,
    ConnectCloudflare,
    ConnectNode,
    IssueConfig,
    MigrateClient,
    SetAdDurations,
    SetPrice,
    SingleValue,
)

router = Router(name="admin")
router.message.filter(F.from_user.id.in_(settings.admin_ids))
router.callback_query.filter(F.from_user.id.in_(settings.admin_ids))


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _valid_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _api_error(exc: httpx.HTTPStatusError) -> str:
    """The server's own `detail` if it sent one -- it's written for a human
    (e.g. "Cloudflare isn't configured yet..."), unlike a bare status code."""
    try:
        return str(exc.response.json().get("detail", exc.response.text))
    except ValueError:
        return exc.response.text or str(exc)


async def _start_wizard(callback: CallbackQuery, state: FSMContext, back: str) -> None:
    """Remembers the message the panel lives in, so every later step of the
    wizard edits that one message instead of spamming new ones."""
    await state.update_data(
        panel_chat=callback.message.chat.id, panel_msg=callback.message.message_id, back=back
    )


async def _panel(message: Message, state: FSMContext, text: str, markup=None) -> None:
    data = await state.get_data()
    chat_id, msg_id = data.get("panel_chat"), data.get("panel_msg")
    if chat_id is None or msg_id is None:
        await message.answer(text, reply_markup=markup, parse_mode="HTML")
        return
    try:
        await message.bot.edit_message_text(
            text, chat_id=chat_id, message_id=msg_id, reply_markup=markup, parse_mode="HTML"
        )
    except Exception:  # noqa: BLE001 -- panel may have been deleted; fall back to a new message
        await message.answer(text, reply_markup=markup, parse_mode="HTML")


async def _scrub(message: Message) -> None:
    """Deletes a message that carried a secret in plaintext (SSH password,
    API token). A bot can delete the user's own message in a private chat."""
    try:
        await message.delete()
    except Exception:  # noqa: BLE001 -- best-effort, never block the flow on it
        pass


async def _finish(message: Message, state: FSMContext, text: str, back: str) -> None:
    # Render before clearing: _panel looks the panel message up in the FSM
    # data, which state.clear() throws away.
    await _panel(message, state, text, kb.back_kb(back))
    await state.clear()


# --------------------------------------------------------------------------
# Navigation
# --------------------------------------------------------------------------


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("🛠 Админ-панель", reply_markup=kb.admin_menu_kb())


@router.callback_query(F.data.in_({"a:menu", "a:nodes", "a:clients", "a:cf"}))
async def cb_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await show_menu(callback.data, callback.message, is_admin=True)
    await callback.answer()


# --------------------------------------------------------------------------
# Nodes -- list / detail / actions
# --------------------------------------------------------------------------


@router.callback_query(F.data == "a:nodes:list")
async def cb_nodes_list(callback: CallbackQuery) -> None:
    await callback.answer()
    nodes = await server_api.list_nodes()
    if not nodes:
        await safe_edit(
            callback.message,
            "Нод пока нет.\n\nДобавьте первую через «➕ Добавить ноду».",
            kb.nodes_menu_kb(),
        )
        return
    await safe_edit(
        callback.message,
        f"📋 <b>Ноды</b> ({len(nodes)})\n\nВыберите ноду, чтобы управлять ей:",
        kb.node_list_kb(nodes),
    )


async def _find_node(node_id: str) -> dict | None:
    return next((n for n in await server_api.list_nodes() if n["id"] == node_id), None)


@router.callback_query(F.data.startswith("a:nd:"))
async def cb_node_detail(callback: CallbackQuery) -> None:
    await callback.answer()
    node_id = callback.data.split(":", 2)[2]
    node = await _find_node(node_id)
    if node is None:
        await safe_edit(callback.message, "Нода не найдена (уже удалена?).", kb.nodes_menu_kb())
        return

    inbound_line = (
        f"SNI: <code>{html.escape(node['sni'])}</code>"
        if node.get("sni")
        else ("Инбаунд создан" if node["has_inbound"] else "⚠️ Инбаунд ещё не создан")
    )
    await safe_edit(
        callback.message,
        f"🖥 <b>{html.escape(node['name'])}</b>\n\n"
        f"IP: <code>{node['ip']}</code>\n"
        f"Страна: {node.get('country') or '—'}\n"
        f"Статус: <b>{node['status']}</b>\n"
        f"Сбоев подряд: {node['consecutive_failures']}\n"
        f"{inbound_line}",
        kb.node_detail_kb(node_id, node["has_inbound"]),
    )


@router.callback_query(F.data.startswith("a:nprov:"))
async def cb_node_provision(callback: CallbackQuery) -> None:
    await callback.answer("Создаю инбаунд...")
    node_id = callback.data.split(":", 2)[2]
    await safe_edit(callback.message, "🔧 Подбираю рабочий SNI и создаю инбаунд...")
    try:
        inbound = await server_api.provision_inbound(node_id, callback.from_user.id)
    except httpx.HTTPStatusError as exc:
        await safe_edit(callback.message, f"❌ Не получилось: {_api_error(exc)}", kb.back_to_admin_kb())
        return
    await safe_edit(
        callback.message,
        f"✅ Инбаунд создан.\n\nПорт: <b>{inbound['port']}</b>\nSNI: <code>{inbound['sni']}</code>",
        kb.back_kb("a:nodes:list"),
    )


@router.callback_query(F.data.startswith("a:nsni:"))
async def cb_node_rotate_sni(callback: CallbackQuery) -> None:
    await callback.answer("Меняю SNI...")
    node_id = callback.data.split(":", 2)[2]
    await safe_edit(callback.message, "🔄 Ищу рабочий SNI...")
    try:
        inbound = await server_api.rotate_sni(node_id, callback.from_user.id)
    except httpx.HTTPStatusError as exc:
        await safe_edit(callback.message, f"❌ Не получилось: {_api_error(exc)}", kb.back_to_admin_kb())
        return
    await safe_edit(
        callback.message,
        f"✅ Новый SNI: <code>{inbound['sni']}</code>\n\n"
        "⚠️ Ранее выданные конфиги для этой ноды перестанут работать — "
        "это неизбежно при смене SNI в REALITY.",
        kb.back_kb("a:nodes:list"),
    )


@router.callback_query(F.data.startswith("a:ndel:"))
async def cb_node_delete_ask(callback: CallbackQuery) -> None:
    await callback.answer()
    node_id = callback.data.split(":", 2)[2]
    node = await _find_node(node_id)
    name = node["name"] if node else node_id
    await safe_edit(
        callback.message,
        f"🗑 Удалить ноду <b>{html.escape(name)}</b>?\n\n"
        "Клиенты на ней потеряют доступ. Отменить будет нельзя.",
        kb.confirm_kb(f"a:ndelyes:{node_id}", f"a:nd:{node_id}"),
    )


@router.callback_query(F.data.startswith("a:ndelyes:"))
async def cb_node_delete(callback: CallbackQuery) -> None:
    await callback.answer("Удаляю...")
    node_id = callback.data.split(":", 2)[2]
    try:
        await server_api.delete_node(node_id, callback.from_user.id)
    except httpx.HTTPStatusError as exc:
        await safe_edit(callback.message, f"❌ Не получилось: {_api_error(exc)}", kb.back_to_admin_kb())
        return
    await safe_edit(callback.message, "✅ Нода удалена.", kb.back_kb("a:nodes"))


# --------------------------------------------------------------------------
# Nodes -- add from scratch (SSH bootstrap)
# --------------------------------------------------------------------------


@router.callback_query(F.data == "a:nodes:add")
async def cb_add_node(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _start_wizard(callback, state, "a:nodes")
    await state.set_state(AddNode.name)
    await safe_edit(
        callback.message,
        "➕ <b>Новая нода</b> (шаг 1 из 4)\n\n"
        "Введите <b>название</b> ноды — как вам удобно её называть.\n\n"
        "Например: <code>nl-1</code>",
        kb.cancel_kb(),
    )


@router.message(AddNode.name)
async def add_node_name(message: Message, state: FSMContext) -> None:
    await state.update_data(name=message.text.strip())
    await state.set_state(AddNode.ip)
    await _panel(
        message,
        state,
        "➕ <b>Новая нода</b> (шаг 2 из 4)\n\n"
        "Введите <b>IP-адрес</b> сервера.\n\n"
        "Например: <code>203.0.113.10</code>",
        kb.cancel_kb(),
    )


@router.message(AddNode.ip)
async def add_node_ip(message: Message, state: FSMContext) -> None:
    ip = message.text.strip()
    if not _valid_ip(ip):
        await _panel(
            message,
            state,
            "➕ <b>Новая нода</b> (шаг 2 из 4)\n\n"
            f"❌ <code>{html.escape(ip)}</code> — не похоже на IP-адрес.\n\n"
            "Введите ещё раз, например: <code>203.0.113.10</code>",
            kb.cancel_kb(),
        )
        return
    await state.update_data(ip=ip)
    await state.set_state(AddNode.ssh_password)
    await _panel(
        message,
        state,
        "➕ <b>Новая нода</b> (шаг 3 из 4)\n\n"
        "Введите <b>root-пароль</b> для SSH-подключения к этому серверу.\n\n"
        "🔒 Сообщение с паролем я сразу удалю из чата, "
        "а сам пароль нигде не сохраню — он нужен только для установки.",
        kb.cancel_kb(),
    )


@router.message(AddNode.ssh_password)
async def add_node_password(message: Message, state: FSMContext) -> None:
    await state.update_data(ssh_password=message.text.strip())
    await _scrub(message)
    await state.set_state(AddNode.country)
    await _panel(
        message,
        state,
        "➕ <b>Новая нода</b> (шаг 4 из 4)\n\n"
        "Введите <b>код страны</b> сервера — балансировщик по нему прикидывает, "
        "кому эта нода будет ближе.\n\n"
        "Например: <code>NL</code>, <code>DE</code>, <code>US</code>",
        kb.skip_or_cancel_kb(),
    )


async def _run_bootstrap(
    message: Message, state: FSMContext, country: str | None, admin_id: int
) -> None:
    data = await state.get_data()
    try:
        node = await server_api.bootstrap_node(
            data["name"], data["ip"], data["ssh_password"], country, admin_id
        )
    except httpx.HTTPStatusError as exc:
        await _finish(message, state, f"❌ Не получилось: {_api_error(exc)}", "a:nodes")
        return
    except httpx.HTTPError as exc:
        await _finish(message, state, f"❌ Сервер не ответил: {exc}", "a:nodes")
        return
    await _finish(
        message,
        state,
        f"🚀 <b>Установка запущена</b>\n\n"
        f"Нода: {html.escape(node['name'])}\n"
        f"IP: <code>{node['ip']}</code>\n\n"
        "Она уже видна в списке со статусом 🟡. Установка 3x-ui занимает "
        "несколько минут — я пришлю отдельное сообщение, когда нода "
        "заработает или если что-то пойдёт не так. Чат можно закрыть.",
        "a:nodes",
    )


@router.message(AddNode.country)
async def add_node_country(message: Message, state: FSMContext) -> None:
    await _run_bootstrap(message, state, message.text.strip().upper()[:2], message.from_user.id)


@router.callback_query(F.data == "skip", StateFilter(AddNode.country))
async def add_node_skip_country(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _run_bootstrap(callback.message, state, None, callback.from_user.id)


# --------------------------------------------------------------------------
# Nodes -- connect a server that already runs 3x-ui
# --------------------------------------------------------------------------


@router.callback_query(F.data == "a:nodes:connect")
async def cb_connect_node(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _start_wizard(callback, state, "a:nodes")
    await state.set_state(ConnectNode.name)
    await safe_edit(
        callback.message,
        "🔗 <b>Подключить готовую ноду</b> (шаг 1 из 6)\n\n"
        "Для сервера, где 3x-ui уже установлен вручную.\n\n"
        "Введите <b>название</b> ноды, например: <code>de-old</code>",
        kb.cancel_kb(),
    )


@router.message(ConnectNode.name)
async def connect_node_name(message: Message, state: FSMContext) -> None:
    await state.update_data(name=message.text.strip())
    await state.set_state(ConnectNode.ip)
    await _panel(
        message,
        state,
        "🔗 <b>Подключить готовую ноду</b> (шаг 2 из 6)\n\n"
        "Введите <b>IP-адрес</b> сервера, например: <code>203.0.113.10</code>",
        kb.cancel_kb(),
    )


@router.message(ConnectNode.ip)
async def connect_node_ip(message: Message, state: FSMContext) -> None:
    ip = message.text.strip()
    if not _valid_ip(ip):
        await _panel(
            message,
            state,
            f"❌ <code>{html.escape(ip)}</code> — не похоже на IP-адрес.\n\n"
            "Введите ещё раз, например: <code>203.0.113.10</code>",
            kb.cancel_kb(),
        )
        return
    await state.update_data(ip=ip)
    await state.set_state(ConnectNode.panel_url)
    await _panel(
        message,
        state,
        "🔗 <b>Подключить готовую ноду</b> (шаг 3 из 6)\n\n"
        "Введите <b>адрес панели</b> 3x-ui вместе с портом.\n\n"
        f"Например: <code>http://{html.escape(ip)}:2053</code>",
        kb.cancel_kb(),
    )


@router.message(ConnectNode.panel_url)
async def connect_node_panel(message: Message, state: FSMContext) -> None:
    await state.update_data(panel_url=message.text.strip())
    await state.set_state(ConnectNode.login)
    await _panel(
        message,
        state,
        "🔗 <b>Подключить готовую ноду</b> (шаг 4 из 6)\n\n"
        "Введите <b>логин</b> от панели 3x-ui.",
        kb.cancel_kb(),
    )


@router.message(ConnectNode.login)
async def connect_node_login(message: Message, state: FSMContext) -> None:
    await state.update_data(login=message.text.strip())
    await state.set_state(ConnectNode.password)
    await _panel(
        message,
        state,
        "🔗 <b>Подключить готовую ноду</b> (шаг 5 из 6)\n\n"
        "Введите <b>пароль</b> от панели 3x-ui.\n\n"
        "🔒 Сообщение с паролем я сразу удалю; в базе он хранится зашифрованным.",
        kb.cancel_kb(),
    )


@router.message(ConnectNode.password)
async def connect_node_password(message: Message, state: FSMContext) -> None:
    await state.update_data(password=message.text.strip())
    await _scrub(message)
    await state.set_state(ConnectNode.country)
    await _panel(
        message,
        state,
        "🔗 <b>Подключить готовую ноду</b> (шаг 6 из 6)\n\n"
        "Введите <b>код страны</b>, например <code>NL</code>.",
        kb.skip_or_cancel_kb(),
    )


async def _run_connect_node(
    message: Message, state: FSMContext, country: str | None, admin_id: int
) -> None:
    data = await state.get_data()
    try:
        node = await server_api.add_node(
            data["name"],
            data["ip"],
            data["panel_url"],
            data["login"],
            data["password"],
            country,
            admin_id,
        )
    except httpx.HTTPStatusError as exc:
        await _finish(message, state, f"❌ Не получилось: {_api_error(exc)}", "a:nodes")
        return
    await _finish(
        message,
        state,
        f"✅ Нода <b>{html.escape(node['name'])}</b> подключена.\n\n"
        "Дальше откройте её в списке и нажмите «🔧 Создать инбаунд».",
        "a:nodes",
    )


@router.message(ConnectNode.country)
async def connect_node_country(message: Message, state: FSMContext) -> None:
    await _run_connect_node(message, state, message.text.strip().upper()[:2], message.from_user.id)


@router.callback_query(F.data == "skip", StateFilter(ConnectNode.country))
async def connect_node_skip_country(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _run_connect_node(callback.message, state, None, callback.from_user.id)


# --------------------------------------------------------------------------
# Clients
# --------------------------------------------------------------------------


@router.callback_query(F.data == "a:cl:issue")
async def cb_issue_config(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _start_wizard(callback, state, "a:clients")
    await state.set_state(IssueConfig.telegram_id)
    await safe_edit(
        callback.message,
        "🎟 <b>Выдать конфиг</b> (шаг 1 из 3)\n\n"
        "Введите <b>Telegram ID</b> пользователя, которому выдаём доступ.\n\n"
        "Например: <code>586528024</code>",
        kb.cancel_kb(),
    )


@router.message(IssueConfig.telegram_id)
async def issue_config_user(message: Message, state: FSMContext) -> None:
    raw = message.text.strip()
    if not raw.lstrip("-").isdigit():
        await _panel(
            message,
            state,
            "❌ Telegram ID — это число.\n\nВведите ещё раз, например: <code>586528024</code>",
            kb.cancel_kb(),
        )
        return
    await state.update_data(telegram_id=int(raw))
    await state.set_state(IssueConfig.hours)
    await _panel(
        message,
        state,
        "🎟 <b>Выдать конфиг</b> (шаг 2 из 3)\n\n"
        "На <b>сколько часов</b> выдать доступ?\n\n"
        "Например: <code>24</code>",
        kb.cancel_kb(),
    )


@router.message(IssueConfig.hours)
async def issue_config_hours(message: Message, state: FSMContext) -> None:
    raw = message.text.strip()
    if not raw.isdigit() or int(raw) == 0:
        await _panel(
            message, state, "❌ Введите целое число часов больше нуля, например <code>24</code>.", kb.cancel_kb()
        )
        return
    await state.update_data(hours=int(raw))

    nodes = [n for n in await server_api.list_nodes() if n["has_inbound"]]
    if not nodes:
        await _finish(message, state, "❌ Нет ни одной ноды с готовым инбаундом.", "a:clients")
        return
    await state.set_state(IssueConfig.node)
    await _panel(
        message,
        state,
        "🎟 <b>Выдать конфиг</b> (шаг 3 из 3)\n\nНа какой ноде выдать?",
        kb.node_picker_kb(nodes, "a:isn"),
    )


@router.callback_query(F.data.startswith("a:isn:"), StateFilter(IssueConfig.node))
async def issue_config_node(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Выдаю...")
    node_id = callback.data.split(":", 2)[2]
    data = await state.get_data()
    try:
        result = await server_api.create_admin_client(
            data["telegram_id"], data["hours"] * 3600, node_id, callback.from_user.id
        )
    except httpx.HTTPStatusError as exc:
        await _finish(callback.message, state, f"❌ Не получилось: {_api_error(exc)}", "a:clients")
        return
    await _finish(
        callback.message,
        state,
        "✅ <b>Конфиг выдан</b>\n\n"
        f"Пользователь: <code>{data['telegram_id']}</code>\n"
        f"Срок: {data['hours']} ч\n\n"
        f"<code>{html.escape(result['vless_uri'])}</code>",
        "a:clients",
    )


@router.callback_query(F.data == "a:cl:migrate")
async def cb_migrate(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _start_wizard(callback, state, "a:clients")
    await state.set_state(MigrateClient.client_id)
    await safe_edit(
        callback.message,
        "🔀 <b>Перенести клиента</b> (шаг 1 из 2)\n\n"
        "Введите <b>ID клиента</b> (UUID из выданного конфига).\n\n"
        "Например: <code>a6da8aea-7a92-47ba-a73b-e89b660423d3</code>",
        kb.cancel_kb(),
    )


@router.message(MigrateClient.client_id)
async def migrate_client_id(message: Message, state: FSMContext) -> None:
    await state.update_data(client_id=message.text.strip())
    nodes = [n for n in await server_api.list_nodes() if n["has_inbound"]]
    await state.set_state(MigrateClient.node)
    await _panel(
        message,
        state,
        "🔀 <b>Перенести клиента</b> (шаг 2 из 2)\n\n"
        "Куда переносим? «Авто» отдаст выбор балансировщику.",
        kb.node_picker_kb(nodes, "a:mgn", with_auto=True),
    )


@router.callback_query(F.data.startswith("a:mgn:"), StateFilter(MigrateClient.node))
async def migrate_client_node(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Переношу...")
    target = callback.data.split(":", 2)[2]
    data = await state.get_data()
    try:
        result = await server_api.migrate_client(
            data["client_id"], callback.from_user.id, None if target == "auto" else target
        )
    except httpx.HTTPStatusError as exc:
        await _finish(callback.message, state, f"❌ Не получилось: {_api_error(exc)}", "a:clients")
        return
    await _finish(
        callback.message,
        state,
        f"✅ <b>Клиент перенесён</b>\n\nНовый конфиг:\n<code>{html.escape(result['vless_uri'])}</code>",
        "a:clients",
    )


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

# One-field settings, so they can share a single wizard state instead of
# each needing its own. `transform` maps what the admin types (days,
# minutes) onto what the server stores (seconds).
SINGLE_SETTINGS: dict[str, dict] = {
    "a:set:subdur": {
        "key": "subscription_duration_seconds",
        "prompt": "⏱ <b>Длительность подписки</b>\n\nСколько <b>дней</b> действует подписка?\n\nНапример: <code>30</code>",
        "numeric": True,
        "transform": lambda v: str(int(v) * 24 * 60 * 60),
        "done": lambda v: f"✅ Длительность подписки: {v} дней",
        "back": "a:settings",
    },
    "a:set:alert": {
        "key": "node_alert_consecutive_failure_threshold",
        "prompt": (
            "🔔 <b>Порог алертов</b>\n\nПосле скольких неудачных проверок подряд "
            "считать ноду проблемной и слать уведомление?\n\nНапример: <code>3</code>"
        ),
        "numeric": True,
        "transform": str,
        "done": lambda v: f"✅ Порог алертов: {v} сбоя(ев) подряд",
        "back": "a:settings",
    },
    "a:set:cbtoken": {
        "key": "cryptobot_api_token",
        "prompt": (
            "🔑 <b>Токен CryptoBot</b>\n\nОткройте @CryptoBot → <code>/pay</code> → "
            "Create App и пришлите сюда полученный токен.\n\n"
            "🔒 Сообщение с токеном я сразу удалю; в базе он хранится зашифрованным."
        ),
        "secret": True,
        "transform": str,
        "done": lambda _v: "✅ Токен CryptoBot сохранён. Оплата подписок включена.",
        "back": "a:settings",
    },
    "a:cf:token": {
        "key": "cloudflare_api_token",
        "prompt": (
            "🔑 <b>API-токен Cloudflare</b>\n\nНужен токен (не Global API Key) с правами "
            "Zone.DNS:Edit и Zone.Zone Settings:Edit.\n\n"
            "Создать: dash.cloudflare.com/profile/api-tokens\n\n"
            "🔒 Сообщение с токеном я сразу удалю; в базе он хранится зашифрованным."
        ),
        "secret": True,
        "transform": str,
        "done": lambda _v: "✅ Токен Cloudflare сохранён.",
        "back": "a:cf",
    },
    "a:cf:zone": {
        "key": "cloudflare_zone_id",
        "prompt": (
            "🌐 <b>Zone ID</b>\n\nОткройте домен в панели Cloudflare — Zone ID указан "
            "на странице Overview справа.\n\nПришлите его сюда."
        ),
        "transform": str,
        "done": lambda v: f"✅ Zone ID сохранён: <code>{html.escape(v)}</code>",
        "back": "a:cf",
    },
}


@router.callback_query(F.data.in_(set(SINGLE_SETTINGS)))
async def cb_single_setting(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    spec = SINGLE_SETTINGS[callback.data]
    await _start_wizard(callback, state, spec["back"])
    await state.update_data(spec_key=callback.data)
    await state.set_state(SingleValue.value)
    await safe_edit(callback.message, spec["prompt"], kb.cancel_kb())


@router.message(SingleValue.value)
async def single_setting_value(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    spec = SINGLE_SETTINGS[data["spec_key"]]
    raw = message.text.strip()

    if spec.get("secret"):
        await _scrub(message)
    if spec.get("numeric") and (not raw.isdigit() or int(raw) == 0):
        await _panel(message, state, "❌ Нужно целое число больше нуля. Попробуйте ещё раз.", kb.cancel_kb())
        return

    try:
        await server_api.set_setting(spec["key"], spec["transform"](raw), message.from_user.id)
    except httpx.HTTPStatusError as exc:
        await _finish(message, state, f"❌ Не получилось: {_api_error(exc)}", spec["back"])
        return
    await _finish(message, state, spec["done"](raw), spec["back"])


@router.callback_query(F.data == "a:set:price")
async def cb_set_price(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _start_wizard(callback, state, "a:settings")
    await state.set_state(SetPrice.amount)
    await safe_edit(
        callback.message,
        "💰 <b>Цена подписки</b> (шаг 1 из 2)\n\nВведите <b>сумму</b>.\n\nНапример: <code>5</code>",
        kb.cancel_kb(),
    )


@router.message(SetPrice.amount)
async def set_price_amount(message: Message, state: FSMContext) -> None:
    raw = message.text.strip().replace(",", ".")
    try:
        if float(raw) <= 0:
            raise ValueError
    except ValueError:
        await _panel(message, state, "❌ Введите положительное число, например <code>5</code>.", kb.cancel_kb())
        return
    await state.update_data(amount=raw)
    await state.set_state(SetPrice.asset)
    await _panel(
        message,
        state,
        "💰 <b>Цена подписки</b> (шаг 2 из 2)\n\nВведите <b>валюту</b>.\n\n"
        "Например: <code>USDT</code>, <code>TON</code>, <code>BTC</code>",
        kb.cancel_kb(),
    )


@router.message(SetPrice.asset)
async def set_price_asset(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    asset = message.text.strip().upper()
    await server_api.set_setting("subscription_price_amount", data["amount"], message.from_user.id)
    await server_api.set_setting("subscription_price_asset", asset, message.from_user.id)
    await _finish(message, state, f"✅ Цена подписки: <b>{data['amount']} {html.escape(asset)}</b>", "a:settings")


@router.callback_query(F.data == "a:set:addur")
async def cb_set_ad_durations(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _start_wizard(callback, state, "a:settings")
    await state.set_state(SetAdDurations.short)
    await safe_edit(
        callback.message,
        "📺 <b>Время за рекламу</b> (шаг 1 из 2)\n\n"
        "Сколько <b>минут</b> VPN давать за <b>короткий</b> ролик?\n\nНапример: <code>15</code>",
        kb.cancel_kb(),
    )


@router.message(SetAdDurations.short)
async def set_ad_short(message: Message, state: FSMContext) -> None:
    raw = message.text.strip()
    if not raw.isdigit() or int(raw) == 0:
        await _panel(message, state, "❌ Нужно целое число минут больше нуля.", kb.cancel_kb())
        return
    await state.update_data(short=int(raw))
    await state.set_state(SetAdDurations.long)
    await _panel(
        message,
        state,
        "📺 <b>Время за рекламу</b> (шаг 2 из 2)\n\n"
        "Сколько <b>минут</b> давать за <b>длинный</b> ролик без перемотки?\n\nНапример: <code>60</code>",
        kb.cancel_kb(),
    )


@router.message(SetAdDurations.long)
async def set_ad_long(message: Message, state: FSMContext) -> None:
    raw = message.text.strip()
    if not raw.isdigit() or int(raw) == 0:
        await _panel(message, state, "❌ Нужно целое число минут больше нуля.", kb.cancel_kb())
        return
    data = await state.get_data()
    await server_api.set_setting("ad_short_duration_seconds", str(data["short"] * 60), message.from_user.id)
    await server_api.set_setting("ad_long_duration_seconds", str(int(raw) * 60), message.from_user.id)
    await _finish(
        message, state, f"✅ За рекламу: {data['short']} мин (короткая) / {raw} мин (длинная)", "a:settings"
    )


# --------------------------------------------------------------------------
# Settings overview -- rendered under the settings menu on entry
# --------------------------------------------------------------------------


_SETTING_LABELS = {
    "subscription_price_amount": "Цена",
    "subscription_price_asset": "Валюта",
    "subscription_duration_seconds": "Подписка",
    "ad_short_duration_seconds": "Короткая реклама",
    "ad_long_duration_seconds": "Длинная реклама",
    "node_alert_consecutive_failure_threshold": "Порог алертов",
    "cryptobot_api_token": "Токен CryptoBot",
    "cloudflare_api_token": "Токен Cloudflare",
    "cloudflare_zone_id": "Cloudflare Zone ID",
}


def _humanize(key: str, value: str) -> str:
    """Seconds are what the server stores; days/minutes are what an admin
    thinks in."""
    if key == "subscription_duration_seconds":
        return f"{int(value) // 86400} дн."
    if key.startswith("ad_") and key.endswith("_duration_seconds"):
        return f"{int(value) // 60} мин."
    return value


@router.callback_query(F.data == "a:settings")
async def cb_settings(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    values = await server_api.list_settings()
    lines = "\n".join(
        f"• {label}: <b>{html.escape(_humanize(key, str(values[key])))}</b>"
        for key, label in _SETTING_LABELS.items()
        if key in values
    )
    await safe_edit(
        callback.message, f"⚙️ <b>Настройки сервиса</b>\n\n{lines}", kb.settings_menu_kb()
    )


# --------------------------------------------------------------------------
# Cloudflare -- connect a domain
# --------------------------------------------------------------------------


@router.callback_query(F.data == "a:cf:connect")
async def cb_cf_connect(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _start_wizard(callback, state, "a:cf")
    await state.set_state(ConnectCloudflare.record_name)
    await safe_edit(
        callback.message,
        "🔗 <b>Подключить домен</b> (шаг 1 из 2)\n\n"
        "Введите <b>домен</b>, который должен указывать на этот сервер.\n\n"
        "Например: <code>vpn.example.com</code>\n\n"
        "⚠️ Токен и Zone ID должны быть заданы заранее в этом же разделе.",
        kb.cancel_kb(),
    )


@router.message(ConnectCloudflare.record_name)
async def cf_record_name(message: Message, state: FSMContext) -> None:
    await state.update_data(record_name=message.text.strip())
    await state.set_state(ConnectCloudflare.server_ip)
    await _panel(
        message,
        state,
        "🔗 <b>Подключить домен</b> (шаг 2 из 2)\n\n"
        "Введите <b>IP этого сервера</b> — или нажмите «Пропустить», "
        "и я определю его сам.",
        kb.skip_or_cancel_kb(),
    )


async def _run_cf_connect(
    message: Message, state: FSMContext, server_ip: str | None, admin_id: int
) -> None:
    data = await state.get_data()
    await _panel(message, state, "☁️ Настраиваю Cloudflare...")
    try:
        result = await server_api.connect_cloudflare(
            data["record_name"], server_ip, admin_id
        )
    except httpx.HTTPStatusError as exc:
        await _finish(message, state, f"❌ Не получилось: {_api_error(exc)}", "a:cf")
        return
    await _finish(
        message,
        state,
        f"✅ <b>Готово</b>\n\n"
        f"{html.escape(result['record_name'])} → <code>{result['server_ip']}</code>\n"
        f"Зона: {html.escape(result['zone_name'])}\n"
        f"Проксирование: {'включено' if result['proxied'] else 'выключено'}\n\n"
        "DNS может обновляться несколько минут.",
        "a:cf",
    )


@router.message(ConnectCloudflare.server_ip)
async def cf_server_ip(message: Message, state: FSMContext) -> None:
    ip = message.text.strip()
    if not _valid_ip(ip):
        await _panel(
            message,
            state,
            f"❌ <code>{html.escape(ip)}</code> — не похоже на IP.\n\n"
            "Введите ещё раз или нажмите «Пропустить».",
            kb.skip_or_cancel_kb(),
        )
        return
    await _run_cf_connect(message, state, ip, message.from_user.id)


@router.callback_query(F.data == "skip", StateFilter(ConnectCloudflare.server_ip))
async def cf_skip_ip(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _run_cf_connect(callback.message, state, None, callback.from_user.id)
