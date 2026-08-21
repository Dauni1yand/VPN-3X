"""User-facing side of the bot: subscription, current config, support.
Everything is buttons -- a user never has to type a command.

Ad-watching itself happens in the Android app, not here; the bot's role per
README is payment + Telegram-based verification + support."""

from __future__ import annotations

import html
from datetime import datetime

import httpx
from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.config import settings
from bot.keyboards import back_to_user_menu_kb, cancel_kb, invoice_kb, user_menu_kb
from bot.menus import safe_edit, show_menu
from bot.services.api_client import server_api
from bot.services.cryptobot import CryptoBotNotConfiguredError, cryptobot
from bot.states import Support

router = Router(name="user")

SUBSCRIPTION_PLAN_CODE = "monthly"

WELCOME = (
    "👋 <b>Привет!</b>\n\n"
    "Это бот VPN-сервиса. Здесь можно оформить подписку, "
    "забрать свой конфиг и написать в поддержку.\n\n"
    "Выберите действие:"
)


def _is_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids


def _format_expiry(iso_ts: str) -> str:
    try:
        return datetime.fromisoformat(iso_ts.replace("Z", "+00:00")).strftime("%d.%m.%Y %H:%M UTC")
    except ValueError:
        return iso_ts


def _config_text(client: dict) -> str:
    return (
        "🔑 <b>Ваш конфиг</b>\n\n"
        f"Действует до: <b>{_format_expiry(client['expires_at'])}</b>\n\n"
        "Скопируйте ссылку и вставьте её в приложение:\n"
        f"<code>{html.escape(client['vless_uri'])}</code>"
    )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        WELCOME, reply_markup=user_menu_kb(_is_admin(message.from_user.id)), parse_mode="HTML"
    )


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Главное меню", reply_markup=user_menu_kb(_is_admin(message.from_user.id)))


@router.callback_query(F.data == "u:menu")
async def cb_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await show_menu("u:menu", callback.message, is_admin=_is_admin(callback.from_user.id))
    await callback.answer()


# --------------------------------------------------------------------------
# Subscription
# --------------------------------------------------------------------------


@router.callback_query(F.data == "u:subscribe")
async def cb_subscribe(callback: CallbackQuery) -> None:
    await callback.answer()
    amount = await server_api.get_setting("subscription_price_amount")
    asset = await server_api.get_setting("subscription_price_asset")

    try:
        invoice = await cryptobot.create_invoice(
            amount=amount,
            asset=asset,
            description="VPN-3X подписка",
            payload=f"{callback.from_user.id}:{SUBSCRIPTION_PLAN_CODE}",
        )
    except CryptoBotNotConfiguredError:
        await safe_edit(
            callback.message,
            "⚠️ Оплата пока недоступна — приём платежей ещё настраивается. Загляните позже.",
            back_to_user_menu_kb(),
        )
        return

    await safe_edit(
        callback.message,
        f"💎 <b>Подписка</b>\n\nК оплате: <b>{amount} {asset}</b>\n\n"
        "Нажмите «Оплатить», а после оплаты — «Я оплатил(а)».\n"
        "Обычно платёж засчитывается автоматически в течение минуты.",
        invoice_kb(invoice["pay_url"], invoice["invoice_id"]),
    )


@router.callback_query(F.data.startswith("u:paid:"))
async def cb_check_payment(callback: CallbackQuery) -> None:
    invoice_id = int(callback.data.split(":")[2])
    status = await cryptobot.get_invoice_status(invoice_id)

    if status != "paid":
        await callback.answer("Оплата ещё не найдена. Попробуйте через минуту.", show_alert=True)
        return

    await callback.answer("Оплата найдена!")
    amount = await server_api.get_setting("subscription_price_amount")
    asset = await server_api.get_setting("subscription_price_asset")
    try:
        result = await server_api.confirm_payment(
            telegram_id=callback.from_user.id,
            provider_invoice_id=str(invoice_id),
            plan_code=SUBSCRIPTION_PLAN_CODE,
            amount=amount,
            currency=asset,
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 409:
            # The webhook already credited this invoice -- show what they have.
            client = await server_api.get_user_client(callback.from_user.id)
            if client:
                await safe_edit(callback.message, _config_text(client), back_to_user_menu_kb())
                return
        raise

    await safe_edit(
        callback.message,
        "✅ <b>Оплата подтверждена!</b>\n\n" + _config_text(result),
        back_to_user_menu_kb(),
    )


@router.callback_query(F.data == "u:config")
async def cb_my_config(callback: CallbackQuery) -> None:
    await callback.answer()
    client = await server_api.get_user_client(callback.from_user.id)
    if client is None:
        await safe_edit(
            callback.message,
            "У вас пока нет активного конфига.\n\nОформите подписку — и он появится здесь.",
            back_to_user_menu_kb(),
        )
        return
    await safe_edit(callback.message, _config_text(client), back_to_user_menu_kb())


# --------------------------------------------------------------------------
# Support
# --------------------------------------------------------------------------


@router.callback_query(F.data == "u:support")
async def cb_support(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Support.message)
    await state.update_data(back="u:menu")
    await safe_edit(
        callback.message,
        "🆘 <b>Поддержка</b>\n\nОпишите проблему одним сообщением — мы передадим её команде.",
        cancel_kb(),
    )
    await callback.answer()


@router.message(Support.message)
async def handle_support_message(message: Message, state: FSMContext) -> None:
    await state.clear()

    if not settings.admin_ids:
        await message.answer("Поддержка пока не настроена, попробуйте позже.", reply_markup=user_menu_kb())
        return

    username = f"@{message.from_user.username}" if message.from_user.username else "без username"
    text = f"🆘 Обращение от {message.from_user.id} ({username}):\n\n{message.text}"

    delivered = False
    for admin_id in settings.admin_ids:
        try:
            await message.bot.send_message(admin_id, text)
            delivered = True
        except Exception:  # noqa: BLE001 -- one admin's chat being unreachable shouldn't skip the rest
            pass

    await message.answer(
        "✅ Сообщение передано в поддержку, скоро ответим."
        if delivered
        else "Не удалось передать сообщение в поддержку, попробуйте позже.",
        reply_markup=user_menu_kb(_is_admin(message.from_user.id)),
    )
