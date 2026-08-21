"""Named menu screens, shared by the navigation buttons and by the cancel
handler, so "back" and "cancel" always land on exactly the same screen the
user would have reached by tapping through."""

from __future__ import annotations

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, Message

from bot import keyboards as kb

_MENUS: dict[str, tuple[str, object]] = {
    "u:menu": ("Главное меню", kb.user_menu_kb),
    "a:menu": ("🛠 Админ-панель", kb.admin_menu_kb),
    "a:nodes": ("🖥 Управление нодами", kb.nodes_menu_kb),
    "a:clients": ("👥 Клиенты", kb.clients_menu_kb),
    "a:settings": ("⚙️ Настройки сервиса", kb.settings_menu_kb),
    "a:cf": ("☁️ Cloudflare", kb.cloudflare_menu_kb),
}


async def safe_edit(
    message: Message,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = "HTML",
) -> None:
    """edit_text, tolerating Telegram's "message is not modified" error --
    re-tapping the button that's already showing isn't a failure worth
    surfacing to the user."""
    try:
        await message.edit_text(text, reply_markup=markup, parse_mode=parse_mode)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise


async def show_menu(target: str, message: Message, *, is_admin: bool = False) -> None:
    title, builder = _MENUS[target]
    markup = builder(is_admin) if target == "u:menu" else builder()
    await safe_edit(message, title, markup)
