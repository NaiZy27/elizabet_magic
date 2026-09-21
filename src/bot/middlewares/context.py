"""Сессия базы и клиент — на каждый апдейт.

Одна транзакция на апдейт: хендлер отработал без исключения — коммит, упал — откат.
Уведомления, поставленные внутри, уходят уже после коммита.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User

from core.db import session_scope
from core.services import orders

logger = logging.getLogger(__name__)


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
            data["customer"] = await orders.get_or_create_customer(
                session,
                telegram_user_id=user.id,
                username=user.username,
                full_name=user.full_name,
            )
            return await handler(event, data)
