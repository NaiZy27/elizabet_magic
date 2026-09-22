"""Отрисовка сценария в одном сообщении.

Клиент проходит оформление, а бот всё это время правит одно и то же сообщение,
а не засыпает чат лентой. Вспомогательные сообщения (фото бокса, карта пункта
выдачи, подсказка к кнопке геопозиции) живут по одному: новое заменяет старое.

Кнопки со старых сообщений снимаются редактированием: удалять сообщения Telegram
разрешает только 48 часов, а менять — без срока. Если выбор сделан, под текстом
остаётся строка-итог («✅ Согласие получено»), чтобы по переписке было видно,
что клиент выбрал.
"""

from __future__ import annotations

import contextlib
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    TelegramObject,
)

from bot.states import (
    BOX_STEPS,
    HELPER_MESSAGE_ID,
    PHOTO_MESSAGE_ID,
    UI_MESSAGE_ID,
    UI_TEXT,
    VENUE_MESSAGE_ID,
)

logger = logging.getLogger(__name__)


def box_header(box_number: int, step: int) -> str:
    """«Бокс 2 · шаг 3 из 6»."""
    return f"Бокс {box_number} · шаг {step} из {BOX_STEPS}"


def compose(header: str | None, title: str, body: str = "") -> str:
    """Текст шага: необязательная строка-подпись, заголовок жирным и тело."""
    top = f"{header}\n<b>{title}</b>" if header else f"<b>{title}</b>"
    return f"{top}\n\n{body}".strip()


async def show(
    event: TelegramObject,
    state: FSMContext,
    *,
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
) -> int | None:
    """Показать шаг: отредактировать сообщение сценария или создать его заново.

    Возвращает id сообщения, в котором показан шаг.
    """
    bot, chat_id = target(event)
    if bot is None or chat_id is None:
        return None

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
            await state.update_data({UI_TEXT: text})
            return message_id
        except TelegramBadRequest as error:
            if "message is not modified" in str(error):
                await state.update_data({UI_TEXT: text})
                return message_id
            # Сообщение удалили или его нельзя править — отправим новое,
            # а у старого на всякий случай снимем кнопки.
            logger.debug("Не получилось отредактировать сообщение сценария: %s", error)
            await strip_buttons(bot, chat_id, message_id)

    try:
        sent = await bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)
    except TelegramBadRequest:
        # Телеграм не принял клавиатуру (например, ссылку на локальный адрес в кнопке).
        # Показать шаг важнее кнопок: отправляем без них, а причину пишем в лог.
        logger.exception("Шаг отправлен без кнопок: Telegram отклонил клавиатуру")
        sent = await bot.send_message(chat_id=chat_id, text=text)
    await state.update_data({UI_MESSAGE_ID: sent.message_id, UI_TEXT: text})
    return sent.message_id


async def retire(
    event: TelegramObject,
    state: FSMContext,
    *,
    footer: str | None = None,
) -> None:
    """Закончить с сообщением сценария: снять кнопки, дописать итог и забыть его.

    Следующий шаг создаст новое сообщение ниже, а по кнопкам старого уже ничего
    не нажать. `footer` — что выбрал клиент: «📍 Пункт выдачи: …».
    """
    bot, chat_id = target(event)
    data = await state.get_data()
    message_id = data.get(UI_MESSAGE_ID)
    if bot is not None and chat_id is not None and message_id is not None:
        text = data.get(UI_TEXT)
        if footer and text:
            await close_message(bot, chat_id, message_id, text=text, footer=footer)
        else:
            await strip_buttons(bot, chat_id, message_id)
    await state.update_data({UI_MESSAGE_ID: None, UI_TEXT: None})


async def mark_choice(message: Message | None, footer: str) -> None:
    """Дописать итог под сообщением, по кнопке которого нажали, и снять кнопки.

    Для сообщений вне сценария (согласие, карточка заказа): их текст берётся
    из самого сообщения.
    """
    if message is None or message.bot is None:
        return
    await close_message(
        message.bot,
        message.chat.id,
        message.message_id,
        text=message.html_text,
        footer=footer,
    )


async def close_message(
    bot: Bot,
    chat_id: int,
    message_id: int,
    *,
    text: str,
    footer: str,
) -> None:
    """Текст + строка-итог, без кнопок. Не вышло (удалено, слишком длинно) — просто снять кнопки."""
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=f"{text}\n\n{footer}",
            reply_markup=None,
        )
    except TelegramBadRequest as error:
        logger.debug("Не получилось дописать итог к сообщению: %s", error)
        await strip_buttons(bot, chat_id, message_id)


async def strip_buttons(bot: Bot, chat_id: int, message_id: int) -> None:
    # Кнопок уже нет или сообщение удалено — это нормально.
    with contextlib.suppress(TelegramBadRequest):
        await bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=None,
        )


async def hide_reply_keyboard(event: TelegramObject) -> None:
    """Убрать клавиатуру под полем ввода (меню, «Отправить телефон»), не оставляя следов.

    Telegram убирает её только сообщением с ReplyKeyboardRemove, поэтому отправляем
    служебное сообщение и сразу удаляем — клавиатура остаётся скрытой.
    """
    bot, chat_id = target(event)
    if bot is None or chat_id is None:
        return
    sent = await bot.send_message(chat_id=chat_id, text="⌛", reply_markup=ReplyKeyboardRemove())
    with contextlib.suppress(TelegramBadRequest):
        await bot.delete_message(chat_id=chat_id, message_id=sent.message_id)


async def show_photo(
    event: TelegramObject,
    state: FSMContext,
    *,
    file_id: str,
    caption: str = "",
) -> None:
    """Показать фото бокса отдельным сообщением, заменив предыдущее."""
    bot, chat_id = target(event)
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
    await _delete_tracked(event, state, PHOTO_MESSAGE_ID)


async def show_venue(
    event: TelegramObject,
    state: FSMContext,
    *,
    latitude: float,
    longitude: float,
    title: str,
    address: str,
) -> None:
    """Пункт выдачи на карте. Одна карта на оформление: новая заменяет прошлую."""
    bot, chat_id = target(event)
    if bot is None or chat_id is None:
        return
    await _delete_tracked(event, state, VENUE_MESSAGE_ID)
    sent = await bot.send_venue(
        chat_id=chat_id,
        latitude=latitude,
        longitude=longitude,
        title=title,
        address=address,
    )
    await state.update_data({VENUE_MESSAGE_ID: sent.message_id})


async def show_helper(
    event: TelegramObject,
    state: FSMContext,
    *,
    text: str,
    keyboard: ReplyKeyboardMarkup | ReplyKeyboardRemove,
) -> None:
    """Сообщение с reply-клавиатурой (телефон, геопозиция). Тоже в одном экземпляре."""
    bot, chat_id = target(event)
    if bot is None or chat_id is None:
        return
    await clear_helper(event, state)
    sent = await bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)
    await state.update_data({HELPER_MESSAGE_ID: sent.message_id})


async def clear_helper(event: TelegramObject, state: FSMContext) -> None:
    await _delete_tracked(event, state, HELPER_MESSAGE_ID)


async def _delete_tracked(event: TelegramObject, state: FSMContext, key: str) -> None:
    bot, chat_id = target(event)
    data = await state.get_data()
    message_id = data.get(key)
    if bot is None or chat_id is None or message_id is None:
        return
    # Старше 48 часов или уже удалено — удалять нечего.
    with contextlib.suppress(TelegramBadRequest):
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    await state.update_data({key: None})


def target(event: TelegramObject) -> tuple[Bot | None, int | None]:
    """Бот и чат, в котором рисуем, — из любого типа события."""
    if isinstance(event, CallbackQuery):
        message = event.message
        return event.bot, message.chat.id if message is not None else None
    if isinstance(event, Message):
        return event.bot, event.chat.id
    return None, None
