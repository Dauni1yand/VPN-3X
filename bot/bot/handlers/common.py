"""Handlers that apply to everyone, admin or not. Included before the
admin/user routers so a wizard started in either one cancels the same way."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from bot.config import settings
from bot.menus import show_menu

router = Router(name="common")


@router.callback_query(F.data == "cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    # Wizards stash where they were launched from, so cancelling a node
    # wizard drops you back on the nodes screen rather than at the very top.
    data = await state.get_data()
    back = data.get("back", "u:menu")
    await state.clear()
    await show_menu(back, callback.message, is_admin=callback.from_user.id in settings.admin_ids)
    await callback.answer("Отменено")
