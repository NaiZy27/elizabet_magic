"""Сессия базы и клиент — на каждый апдейт.

Одна транзакция на апдейт: хендлер отработал без исключения — коммит, упал — откат.
Исключение — сбой самого Telegram при ответе клиенту: тогда коммит (см. TELEGRAM_OUTAGE).
Уведомления, поставленные внутри, уходят уже после коммита.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.types import TelegramObject, User

from core.config import get_settings
from core.db import session_scope
from core.services import orders

logger = logging.getLogger(__name__)

#: Сбои на стороне Telegram (сеть, 5xx, «слишком часто») — не ошибка в наших данных.
TELEGRAM_OUTAGE = (TelegramNetworkError, TelegramServerError, TelegramRetryAfter)


class ContextMiddleware(BaseMiddleware):
    """Кладёт в данные хендлера `session` и `customer`."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None or user.is_bot:
            return await handler(event, data)

        async with session_scope() as session:
            data["session"] = session
            if user.id not in get_settings().staff_chat_ids:
                # Владелице карточка клиента не нужна: она не заказывает,
                # а в базе клиентов должны быть только покупатели.
                data["customer"] = await orders.get_or_create_customer(
                    session,
                    telegram_user_id=user.id,
                    username=user.username,
                    full_name=user.full_name,
                )
            try:
                return await handler(event, data)
            except TELEGRAM_OUTAGE as error:
                # Telegram не принял ответ бота — действие клиента (пункт, получатель,
                # оформление) всё равно сохраняем: при следующем нажатии экран соберётся
                # из базы. Откат стёр бы то, что клиент уже сделал.
                logger.warning("Telegram не ответил, апдейт сохранён без ответа: %r", error)
                return None
