import asyncio
import logging

from aiogram import Bot, Dispatcher

from bot.config import settings
from bot.handlers import admin, user


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    bot = Bot(token=settings.bot_token)
    dp = Dispatcher()
    dp.include_router(admin.router)
    dp.include_router(user.router)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
