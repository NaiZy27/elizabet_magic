"""Что делать с сообщением, которое не подошло ни одному шагу."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from bot import ui
from bot.keyboards.common import main_menu

logger = logging.getLogger(__name__)

router = Router(name="fallback")


@router.callback_query()
async def stale_button(callback: CallbackQuery) -> None:
    """Кнопка из старого сообщения: снимаем кнопки и отвечаем, чтобы не крутился индикатор."""
    await callback.answer("Это сообщение устарело — откройте меню заново.")
    await ui.reply(callback, text="Главное меню", keyboard=main_menu())


@router.message(F.text)
async def unknown_text(message: Message) -> None:
    await ui.reply(message, text="Не понял вас. Выберите пункт меню 💗", keyboard=main_menu())
