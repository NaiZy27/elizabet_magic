"""Отрисовка сценария в одном сообщении.

Клиент проходит восемь шагов, и всё это время бот правит одно и то же сообщение,
а не засыпает чат лентой. Здесь же — общий заголовок с номером шага.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, TelegramObject

from bot.states import PHOTO_MESSAGE_ID, UI_MESSAGE_ID

logger = logging.getLogger(__name__)

#: Сколько шагов в оформлении — показывается клиенту как «Шаг 3 из 8».
TOTAL_STEPS = 8


def step_header(step: int | None, title: str) -> str:
    if step is None:
        return f"<b>{title}</b>"
    return f"Шаг {step} из {TOTAL_STEPS}\n<b>{title}</b>"


def compose(step: int | None, title: str, body: str = "") -> str:
    header = step_header(step, title)
    return f"{header}\n\n{body}".strip()


async def show(
    event: TelegramObject,
    state: FSMContext,
    *,
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
) -> None:
    """Показать шаг: отредактировать сообщение сценария или создать его заново."""
    bot, chat_id = _target(event)
    if bot is None or chat_id is None:
        return

    data = await state.get_data()
    message_id = data.get(UI_MESSAGE_ID)

    if message_id is not None:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=keyboard,
            )
            return
        except TelegramBadRequest as error:
            if "message is not modified" in str(error):
                return
            # Сообщение удалили или оно слишком старое — отправим новое.
            logger.debug("Не получилось отредактировать сообщение сценария: %s", error)

    sent = await bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)
    await state.update_data({UI_MESSAGE_ID: sent.message_id})


async def show_photo(
    event: TelegramObject,
    state: FSMContext,
    *,
    file_id: str,
    caption: str = "",
) -> None:
    """Показать фото бокса отдельным сообщением, заменив предыдущее."""
    bot, chat_id = _target(event)
    if bot is None or chat_id is None:
        return

    await clear_photo(event, state)
    try:
        sent = await bot.send_photo(chat_id=chat_id, photo=file_id, caption=caption)
    except TelegramBadRequest as error:
        # Фото могло быть загружено другим ботом — file_id тогда не работает.
        logger.warning("Не удалось показать фото бокса: %s", error)
        return
    await state.update_data({PHOTO_MESSAGE_ID: sent.message_id})


async def clear_photo(event: TelegramObject, state: FSMContext) -> None:
    """Убрать фото предыдущего шага, чтобы чат не зарастал картинками."""
    bot, chat_id = _target(event)
    data = await state.get_data()
    message_id = data.get(PHOTO_MESSAGE_ID)
    if bot is None or chat_id is None or message_id is None:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramBadRequest:
        pass
    await state.update_data({PHOTO_MESSAGE_ID: None})


async def forget_ui_message(state: FSMContext) -> None:
    """Забыть сообщение сценария: следующий шаг создаст новое."""
    await state.update_data({UI_MESSAGE_ID: None})


def _target(event: TelegramObject) -> tuple[Bot | None, int | None]:
    """Бот и чат, в котором рисуем, — из любого типа события."""
    if isinstance(event, CallbackQuery):
        message = event.message
        return event.bot, message.chat.id if message is not None else None
    if isinstance(event, Message):
        return event.bot, event.chat.id
    return None, None
