"""Запуск клиентского бота.

На старте — поллинг: он не требует домена и сертификата. Перевод на вебхук,
если понадобится, затронет только этот файл.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import BotCommand

from bot.handlers import build_router
from bot.middlewares import ContextMiddleware
from core.config import get_settings
from core.db import dispose_engine
from worker.broker import ensure_started, shutdown

logger = logging.getLogger(__name__)

COMMANDS = [
    BotCommand(command="start", description="Начать сначала"),
    BotCommand(command="menu", description="Главное меню"),
]


def create_dispatcher() -> Dispatcher:
    settings = get_settings()
    # FSM в Redis: клиент продолжит оформление даже после перезапуска бота.
    storage = RedisStorage.from_url(settings.redis_fsm_url)
    dispatcher = Dispatcher(storage=storage)

    context = ContextMiddleware()
    dispatcher.message.middleware(context)
    dispatcher.callback_query.middleware(context)

    dispatcher.include_router(build_router())
    return dispatcher


async def run() -> None:
    settings = get_settings()
    token = settings.bot.token.get_secret_value()
    if not token:
        raise RuntimeError("BOT_TOKEN не задан — бот не может запуститься")

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = create_dispatcher()

    # Брокер нужен, чтобы бот мог ставить задачи уведомлений.
    await ensure_started()
    await bot.set_my_commands(COMMANDS)

    try:
        logger.info("Бот запущен")
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        await bot.session.close()
        await shutdown()
        await dispose_engine()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()
