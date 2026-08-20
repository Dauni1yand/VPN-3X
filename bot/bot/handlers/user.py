"""User-facing commands: paid subscription (via CryptoBot) and support.
Ad-viewing itself happens in the Android app, not here -- the bot's role per
README is limited to payment + Telegram-based verification + support."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.services.api_client import server_api
from bot.services.cryptobot import cryptobot

router = Router(name="user")

# TODO(Etap 3/6): price and plan_code should come from the server's
# admin-tunable `settings` table, not be hardcoded here.
SUBSCRIPTION_PLAN_CODE = "monthly"
SUBSCRIPTION_PRICE_AMOUNT = "5"
SUBSCRIPTION_PRICE_ASSET = "USDT"


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Привет! Это бот VPN-сервиса.\n"
        "/subscribe -- оформить платную подписку через CryptoBot\n"
        "/support -- написать в поддержку"
    )


@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message) -> None:
    invoice = await cryptobot.create_invoice(
        amount=SUBSCRIPTION_PRICE_AMOUNT,
        asset=SUBSCRIPTION_PRICE_ASSET,
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
        f"Счёт на {SUBSCRIPTION_PRICE_AMOUNT} {SUBSCRIPTION_PRICE_ASSET} создан.", reply_markup=keyboard
    )


@router.callback_query(F.data.startswith("check_payment:"))
async def cb_check_payment(callback: CallbackQuery) -> None:
    invoice_id = int(callback.data.split(":", 1)[1])
    status = await cryptobot.get_invoice_status(invoice_id)

    if status != "paid":
        await callback.answer("Оплата ещё не найдена, попробуйте позже.", show_alert=True)
        return

    result = await server_api.confirm_payment(
        telegram_id=callback.from_user.id,
        provider_invoice_id=str(invoice_id),
        plan_code=SUBSCRIPTION_PLAN_CODE,
        amount=SUBSCRIPTION_PRICE_AMOUNT,
        currency=SUBSCRIPTION_PRICE_ASSET,
    )
    await callback.message.answer(f"Оплата подтверждена!\nКонфиг: `{result['vless_uri']}`", parse_mode="Markdown")
    await callback.answer()


@router.message(Command("support"))
async def cmd_support(message: Message) -> None:
    # TODO(Etap 3): forward to an actual support queue/admin chat instead of
    # just acknowledging -- this is a placeholder.
    await message.answer("Опишите проблему одним сообщением, мы её передадим в поддержку.")
