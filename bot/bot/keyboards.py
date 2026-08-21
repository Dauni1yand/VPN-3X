"""Inline keyboards for the whole bot.

Callback data is `section:action[:arg]`. Node ids are UUIDs (36 chars), so
even the longest form here stays well inside Telegram's 64-byte limit on
callback_data."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

CANCEL = "❌ Отмена"
BACK = "⬅️ Назад"


def _rows(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=list(rows))


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


# --------------------------------------------------------------------------
# Shared
# --------------------------------------------------------------------------


def cancel_kb() -> InlineKeyboardMarkup:
    """Shown under every wizard prompt so there's always a way out that
    isn't "send a command and hope"."""
    return _rows([btn(CANCEL, "cancel")])


def skip_or_cancel_kb() -> InlineKeyboardMarkup:
    return _rows([btn("⏭ Пропустить", "skip")], [btn(CANCEL, "cancel")])


# --------------------------------------------------------------------------
# User side
# --------------------------------------------------------------------------


def user_menu_kb(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [btn("💎 Оформить подписку", "u:subscribe")],
        [btn("🔑 Мой конфиг", "u:config")],
        [btn("🆘 Поддержка", "u:support")],
    ]
    if is_admin:
        rows.append([btn("🛠 Админ-панель", "a:menu")])
    return _rows(*rows)


def back_to_user_menu_kb() -> InlineKeyboardMarkup:
    return _rows([btn(BACK, "u:menu")])


def invoice_kb(pay_url: str, invoice_id: int) -> InlineKeyboardMarkup:
    return _rows(
        [InlineKeyboardButton(text="💳 Оплатить", url=pay_url)],
        [btn("✅ Я оплатил(а)", f"u:paid:{invoice_id}")],
        [btn(BACK, "u:menu")],
    )


# --------------------------------------------------------------------------
# Admin side
# --------------------------------------------------------------------------


def admin_menu_kb() -> InlineKeyboardMarkup:
    return _rows(
        [btn("🖥 Ноды", "a:nodes"), btn("👥 Клиенты", "a:clients")],
        [btn("⚙️ Настройки", "a:settings"), btn("☁️ Cloudflare", "a:cf")],
        [btn(BACK, "u:menu")],
    )


def back_to_admin_kb() -> InlineKeyboardMarkup:
    return _rows([btn(BACK, "a:menu")])


def back_kb(target: str) -> InlineKeyboardMarkup:
    """A lone "back" button pointing at any menu target."""
    return _rows([btn(BACK, target)])


def nodes_menu_kb() -> InlineKeyboardMarkup:
    return _rows(
        [btn("📋 Список нод", "a:nodes:list")],
        [btn("➕ Добавить ноду (с нуля)", "a:nodes:add")],
        [btn("🔗 Подключить готовую 3x-ui", "a:nodes:connect")],
        [btn(BACK, "a:menu")],
    )


def node_list_kb(nodes: list[dict]) -> InlineKeyboardMarkup:
    status_icon = {"active": "🟢", "unstable": "🔴", "provisioning": "🟡", "retired": "⚫️"}
    rows = [
        [btn(f"{status_icon.get(n['status'], '❔')} {n['name']} ({n['ip']})", f"a:nd:{n['id']}")]
        for n in nodes
    ]
    rows.append([btn(BACK, "a:nodes")])
    return _rows(*rows)


def node_detail_kb(node_id: str, has_inbound: bool) -> InlineKeyboardMarkup:
    rows = []
    if has_inbound:
        rows.append([btn("🔄 Сменить SNI", f"a:nsni:{node_id}")])
    else:
        rows.append([btn("🔧 Создать инбаунд", f"a:nprov:{node_id}")])
    rows.append([btn("🗑 Удалить ноду", f"a:ndel:{node_id}")])
    rows.append([btn(BACK, "a:nodes:list")])
    return _rows(*rows)


def confirm_kb(yes_data: str, no_data: str) -> InlineKeyboardMarkup:
    return _rows([btn("✅ Да, удалить", yes_data)], [btn("↩️ Нет", no_data)])


def clients_menu_kb() -> InlineKeyboardMarkup:
    return _rows(
        [btn("🎟 Выдать конфиг", "a:cl:issue")],
        [btn("🔀 Перенести клиента", "a:cl:migrate")],
        [btn(BACK, "a:menu")],
    )


def settings_menu_kb() -> InlineKeyboardMarkup:
    return _rows(
        [btn("💰 Цена подписки", "a:set:price")],
        [btn("⏱ Длительность подписки", "a:set:subdur")],
        [btn("📺 Время за рекламу", "a:set:addur")],
        [btn("🔔 Порог алертов", "a:set:alert")],
        [btn("🔑 Токен CryptoBot", "a:set:cbtoken")],
        [btn(BACK, "a:menu")],
    )


def cloudflare_menu_kb() -> InlineKeyboardMarkup:
    return _rows(
        [btn("🔑 API-токен", "a:cf:token")],
        [btn("🌐 Zone ID", "a:cf:zone")],
        [btn("🔗 Подключить домен", "a:cf:connect")],
        [btn(BACK, "a:menu")],
    )


def node_picker_kb(nodes: list[dict], prefix: str, *, with_auto: bool = False) -> InlineKeyboardMarkup:
    """Node chooser used by the issue-config / migrate wizards, so an admin
    picks a node from a list instead of pasting a UUID."""
    rows = [[btn(f"{n['name']} ({n['ip']})", f"{prefix}:{n['id']}")] for n in nodes]
    if with_auto:
        rows.insert(0, [btn("🎲 Авто (балансировщик)", f"{prefix}:auto")])
    rows.append([btn(CANCEL, "cancel")])
    return _rows(*rows)
