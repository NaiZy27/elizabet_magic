"""Апдейты одного пользователя — строго по очереди.

aiogram обрабатывает апдейты параллельно, и два быстрых нажатия одного клиента
шли бы одновременно: каждое читает черновик и FSM до того, как другое их сохранило.
Так двойной тап на последнем шаге бокса клал в заказ два одинаковых бокса, а быстрые
отметки цветов теряли друг друга. Здесь апдейты одного пользователя выстраиваются
в очередь, а разных пользователей по-прежнему идут параллельно.

Блокировки живут в памяти процесса: бот запускается в одном экземпляре. Если когда-
нибудь ботов станет несколько, нужна распределённая блокировка (например, в Redis).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User


class _UserLock:
    __slots__ = ("lock", "users")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        #: Сколько апдейтов сейчас держат или ждут эту блокировку.
        self.users = 0


class SerialPerUserMiddleware(BaseMiddleware):
    def __init__(self) -> None:
        self._locks: dict[int, _UserLock] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        entry = self._locks.setdefault(user.id, _UserLock())
        entry.users += 1
        try:
            async with entry.lock:
                return await handler(event, data)
        finally:
            entry.users -= 1
            # Последний ушедший убирает блокировку, чтобы словарь не рос бесконечно.
            if entry.users == 0 and self._locks.get(user.id) is entry:
                del self._locks[user.id]
