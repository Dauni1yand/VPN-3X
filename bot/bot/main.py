import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from bot.config import settings
from bot.handlers import admin, user


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    bot = Bot(token=settings.bot_token)
    # MemoryStorage is fine as long as the bot runs as a single process (it
    # is, per PLAN.md -- unlike the main server, nothing calls for the bot to
    # be scaled out horizontally); state wouldn't survive a multi-instance
    # bot deployment.
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(admin.router)
    dp.include_router(user.router)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
