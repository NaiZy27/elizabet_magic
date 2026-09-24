"""Фильтры обработчиков."""

from __future__ import annotations

from aiogram.filters import Filter
from aiogram.types import TelegramObject, User

from core.config import get_settings


class IsStaff(Filter):
    """Сообщение от владелицы или из служебного чата — по id из .env.

    Такому аккаунту бот показывает рабочее меню: фото боксов, ссылка на панель.
    Клиентский сценарий заказа для него закрыт.
    """

    def __init__(self, *, staff: bool = True) -> None:
        self.staff = staff

    async def __call__(self, _event: TelegramObject, event_from_user: User | None = None) -> bool:
        if event_from_user is None:
            return not self.staff
        return (event_from_user.id in get_settings().staff_chat_ids) is self.staff
