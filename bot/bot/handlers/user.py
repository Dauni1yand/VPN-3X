"""User-facing commands: paid subscription (via CryptoBot) and support.
Ad-viewing itself happens in the Android app, not here -- the bot's role per
README is limited to payment + Telegram-based verification + support."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.config import settings
from bot.services.api_client import server_api
from bot.services.cryptobot import cryptobot

router = Router(name="user")


class SupportForm(StatesGroup):
    waiting_for_message = State()

SUBSCRIPTION_PLAN_CODE = "monthly"


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Привет! Это бот VPN-сервиса.\n"
        "/subscribe -- оформить платную подписку через CryptoBot\n"
        "/support -- написать в поддержку"
    )


@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message) -> None:
    amount = await server_api.get_setting("subscription_price_amount")
    asset = await server_api.get_setting("subscription_price_asset")

    invoice = await cryptobot.create_invoice(
        amount=amount,
        asset=asset,
        description="VPN-3X подписка",
        payload=f"{message.from_user.id}:{SUBSCRIPTION_PLAN_CODE}",
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Оплатить", url=invoice["pay_url"])],
            [
                InlineKeyboardButton(
                    text="Я оплатил(а)",
                    callback_data=f"check_payment:{invoice['invoice_id']}",
                )
            ],
        ]
    )
    await message.answer(
        f"Счёт на {amount} {asset} создан.\n"
        "Оплата обычно засчитывается автоматически в течение минуты, "
        "кнопка «Я оплатил(а)» — на случай, если хочется проверить сразу.",
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("check_payment:"))
async def cb_check_payment(callback: CallbackQuery) -> None:
    invoice_id = int(callback.data.split(":", 1)[1])
    status = await cryptobot.get_invoice_status(invoice_id)

    if status != "paid":
        await callback.answer("Оплата ещё не найдена, попробуйте позже.", show_alert=True)
        return

    amount = await server_api.get_setting("subscription_price_amount")
    asset = await server_api.get_setting("subscription_price_asset")
    result = await server_api.confirm_payment(
        telegram_id=callback.from_user.id,
        provider_invoice_id=str(invoice_id),
        plan_code=SUBSCRIPTION_PLAN_CODE,
        amount=amount,
        currency=asset,
    )
    await callback.message.answer(f"Оплата подтверждена!\nКонфиг: `{result['vless_uri']}`", parse_mode="Markdown")
    await callback.answer()


@router.message(Command("support"))
async def cmd_support(message: Message, state: FSMContext) -> None:
    await state.set_state(SupportForm.waiting_for_message)
    await message.answer("Опишите проблему одним сообщением, мы её передадим в поддержку.")


@router.message(SupportForm.waiting_for_message)
async def handle_support_message(message: Message, state: FSMContext) -> None:
    await state.clear()

    if not settings.admin_ids:
        await message.answer("Поддержка пока не настроена, попробуйте позже.")
        return

    forwarded_to_any = False
    for admin_id in settings.admin_ids:
        try:
            await message.bot.send_message(
                admin_id,
                f"Обращение в поддержку от {message.from_user.id} (@{message.from_user.username}):\n\n{message.text}",
            )
            forwarded_to_any = True
        except Exception:  # noqa: BLE001 -- one admin's chat being unreachable shouldn't fail the others
            pass

    if forwarded_to_any:
        await message.answer("Сообщение передано в поддержку, скоро ответим.")
    else:
        await message.answer("Не удалось передать сообщение в поддержку, попробуйте позже.")
