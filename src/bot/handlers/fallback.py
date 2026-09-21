"""Что делать с сообщением, которое не подошло ни одному шагу."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from bot.keyboards.common import main_menu

logger = logging.getLogger(__name__)

router = Router(name="fallback")


@router.callback_query()
async def stale_button(callback: CallbackQuery) -> None:
    """Кнопка из старого сообщения: отвечаем, чтобы у клиента не крутился индикатор."""
    await callback.answer("Это сообщение устарело. Откройте меню и начните заново.")


@router.message(F.text)
async def unknown_text(message: Message) -> None:
    await message.answer(
        "Не понял вас. Выберите пункт меню ниже — или напишите нам, "
        "если нужен живой человек 💗",
        reply_markup=main_menu(),
    )
