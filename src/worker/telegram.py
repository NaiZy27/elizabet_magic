"""Отправка сообщений клиентам из фоновых задач.

Пишем клиенту только токеном клиентского бота: chat_id привязан к конкретному боту,
чужим токеном сообщение не уйдёт.
"""

from __future__ import annotations

from functools import lru_cache

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from core.config import get_settings


@lru_cache(maxsize=1)
def get_bot() -> Bot:
    settings = get_settings()
    token = settings.bot.token.get_secret_value()
    if not token:
        raise RuntimeError("BOT_TOKEN не задан: отправлять сообщения нечем")
    return Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


async def close_bot() -> None:
    """Закрыть HTTP-сессию бота при остановке процесса."""
    if get_bot.cache_info().currsize:
        await get_bot().session.close()
        get_bot.cache_clear()
