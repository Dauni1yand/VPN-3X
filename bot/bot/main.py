import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, ErrorEvent, Message

from bot.config import settings
from bot.handlers import admin, common, user
from bot.services.api_client import server_api

logger = logging.getLogger(__name__)

# What a user sees when a handler dies on something we didn't anticipate.
# Deliberately not the exception text: this reaches end users too, and a
# traceback fragment tells them nothing while potentially leaking internals.
UNEXPECTED = (
    "⚠️ Что-то пошло не так. Попробуйте ещё раз или напишите в поддержку."
)


async def on_error(event: ErrorEvent) -> bool:
    """Turns an unhandled handler exception into a visible message.

    Without this aiogram logs the traceback and drops the update, so from
    the user's side the button they tapped simply does nothing -- which is
    indistinguishable from the bot being down, and is how a stopped main
    server used to present itself.
    """
    logger.exception("unhandled error while handling update", exc_info=event.exception)

    update = event.update
    target: Message | None = None

    if isinstance(getattr(update, "callback_query", None), CallbackQuery):
        callback = update.callback_query
        try:
            # Always answer the callback, or the client keeps showing a
            # spinner on the button until it times out.
            await callback.answer(UNEXPECTED, show_alert=True)
        except Exception:  # noqa: BLE001 -- already in the error path
            pass
        target = callback.message
    elif isinstance(getattr(update, "message", None), Message):
        target = update.message

    if target is not None:
        try:
            await target.answer(UNEXPECTED)
        except Exception:  # noqa: BLE001 -- nothing left to try
            pass

    return True


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    bot = Bot(token=settings.bot_token)
    # MemoryStorage is fine as long as the bot runs as a single process (it
    # is, per PLAN.md -- unlike the main server, nothing calls for the bot to
    # be scaled out horizontally); state wouldn't survive a multi-instance
    # bot deployment.
    dp = Dispatcher(storage=MemoryStorage())
    # common first: it owns "cancel", which has to work identically whether
    # the wizard being cancelled was started from the admin or the user side.
    dp.include_router(common.router)
    dp.include_router(admin.router)
    dp.include_router(user.router)
    dp.errors.register(on_error)

    try:
        await dp.start_polling(bot)
    finally:
        # One shared httpx client serves every call to the main server;
        # closing it lets in-flight connections drain on shutdown.
        await server_api.aclose()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
