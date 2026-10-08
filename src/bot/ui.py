"""Отрисовка сценария в одном сообщении.

Клиент проходит оформление, а бот всё это время правит одно и то же сообщение,
а не засыпает чат лентой. Сообщения — rich (Bot API 10.3): кнопки стоят внутри
самого сообщения, между абзацами, а не клавиатурой под ним. Вспомогательные
сообщения (фото бокса, кнопка геопозиции или телефона под полем ввода) живут
по одному: новое заменяет старое.

Кнопки со старых сообщений снимаются редактированием: в rich-сообщении кнопки —
часть текста, поэтому сообщение переписывается целиком без них. Для этого рядом
с id сообщения хранится его текст без кнопок (UI_TEXT, уже в rich-HTML).
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
    InputRichMessage,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    TelegramObject,
)

from bot import rich
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
    """«Бокс 2 · шаг 3 из 4»."""
    return f"Бокс {box_number} · шаг {step} из {BOX_STEPS}"


def compose(header: str | None, title: str, body: str = "") -> str:
    """Текст шага: необязательная строка-подпись, заголовок жирным и тело."""
    top = f"{header}\n<b>{title}</b>" if header else f"<b>{title}</b>"
    return f"{top}\n\n{body}".strip()


def _not_modified(error: TelegramBadRequest) -> bool:
    return "message is not modified" in str(error)


async def _edit_rich(
    bot: Bot,
    chat_id: int,
    message_id: int,
    rich_message: InputRichMessage,
) -> bool:
    """Переписать сообщение rich-содержимым. False — Telegram не дал (удалено, старое)."""
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            rich_message=rich_message,
        )
        return True
    except TelegramBadRequest as error:
        if _not_modified(error):
            return True
        logger.debug("Не получилось переписать сообщение %s: %s", message_id, error)
        return False


async def send(
    bot: Bot,
    chat_id: int,
    *,
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
    note: str | None = None,
) -> Message:
    """Отправить сообщение с кнопками внутри. Не принял rich — обычное с клавиатурой."""
    try:
        return await bot.send_rich_message(
            chat_id=chat_id,
            rich_message=rich.build(text, keyboard, note=note),
        )
    except TelegramBadRequest:
        logger.exception("Telegram не принял rich-сообщение — отправляем обычное")
    full_text = f"{text}\n\n{note}" if note else text
    try:
        return await bot.send_message(
            chat_id=chat_id,
            text=full_text,
            reply_markup=rich.classic_keyboard(keyboard),
            disable_web_page_preview=True,
        )
    except TelegramBadRequest:
        logger.exception("Шаг отправлен без кнопок: Telegram отклонил клавиатуру")
        return await bot.send_message(chat_id=chat_id, text=full_text)


async def show(
    event: TelegramObject,
    state: FSMContext,
    *,
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
    note: str | None = None,
) -> int | None:
    """Показать шаг: переписать сообщение сценария или создать его заново.

    Возвращает id сообщения, в котором показан шаг.
    """
    bot, chat_id = target(event)
    if bot is None or chat_id is None:
        return None

    body = rich.text_html(text)
    data = await state.get_data()
    message_id = data.get(UI_MESSAGE_ID)
    if message_id is not None:
        if await _edit_rich(bot, chat_id, message_id, rich.build(text, keyboard, note=note)):
            await state.update_data({UI_TEXT: body})
            return message_id
        # Сообщение удалили или оно обычное, старого образца, — шлём новое,
        # а со старого снимаем кнопки.
        await strip_buttons(bot, chat_id, message_id, body=data.get(UI_TEXT))

    sent = await send(bot, chat_id, text=text, keyboard=keyboard, note=note)
    await state.update_data({UI_MESSAGE_ID: sent.message_id, UI_TEXT: body})
    return sent.message_id


def message_body(message: Message) -> str | None:
    """Текст сообщения без кнопок в rich-HTML — чтобы потом переписать его без кнопок."""
    if message.rich_message is not None:
        return rich.body_html(message.rich_message)
    if message.text is not None:
        return rich.text_html(message.html_text)
    return None


async def adopt(event: TelegramObject, state: FSMContext) -> None:
    """Сделать сообщение, по кнопке которого нажали, сообщением сценария.

    Так «Заказать бокс» из меню продолжает оформление в том же сообщении, а не
    присылает новое. Фото и прочие сообщения без текста не подходят.
    """
    if not isinstance(event, CallbackQuery) or not isinstance(event.message, Message):
        return
    body = message_body(event.message)
    if body is None:
        return
    await state.update_data({UI_MESSAGE_ID: event.message.message_id, UI_TEXT: body})


async def reply(
    event: TelegramObject,
    *,
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
    note: str | None = None,
) -> Message | None:
    """Ответ вне сценария: по кнопке — переписать её сообщение, на текст — новым сообщением."""
    bot, chat_id = target(event)
    if bot is None or chat_id is None:
        return None
    if isinstance(event, CallbackQuery) and isinstance(event.message, Message):
        message = event.message
        if await _edit_rich(
            bot, chat_id, message.message_id, rich.build(text, keyboard, note=note)
        ):
            return message
        await strip_buttons(bot, chat_id, message.message_id, body=message_body(message))
    return await send(bot, chat_id, text=text, keyboard=keyboard, note=note)


async def consume(message: Message, state: FSMContext) -> None:
    """Ответ клиента принят: убрать его сообщение, чтобы шаги шли в одном сообщении бота.

    Бот вправе удалять входящие сообщения в личном чате. Не вышло — тогда ответ
    остаётся в чате, а следующий шаг появится новым сообщением под ним.
    """
    if message.bot is None:
        return
    try:
        await message.bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    except TelegramBadRequest as error:
        logger.debug("Не удалось убрать ответ клиента: %s", error)
        await retire(message, state)


async def retire(
    event: TelegramObject,
    state: FSMContext,
    *,
    footer: str | None = None,
) -> None:
    """Закончить с сообщением сценария: снять кнопки, дописать итог и забыть его.

    Следующий шаг создаст новое сообщение ниже, а по кнопкам старого уже ничего
    не нажать. `footer` — что выбрал клиент.
    """
    bot, chat_id = target(event)
    data = await state.get_data()
    message_id = data.get(UI_MESSAGE_ID)
    if bot is not None and chat_id is not None and message_id is not None:
        await close_message(bot, chat_id, message_id, body=data.get(UI_TEXT), footer=footer)
    await state.update_data({UI_MESSAGE_ID: None, UI_TEXT: None})


async def mark_choice(message: Message | None, footer: str) -> None:
    """Дописать итог под сообщением, по кнопке которого нажали, и снять кнопки."""
    if message is None or message.bot is None:
        return
    await close_message(
        message.bot,
        message.chat.id,
        message.message_id,
        body=message_body(message),
        footer=footer,
    )


async def close_message(
    bot: Bot,
    chat_id: int,
    message_id: int,
    *,
    body: str | None,
    footer: str | None = None,
) -> None:
    """Текст + строка-итог, без кнопок."""
    if body:
        html = body + (rich.text_html(footer) if footer else "")
        rich_message = InputRichMessage(html=html, skip_entity_detection=True)
        if await _edit_rich(bot, chat_id, message_id, rich_message):
            return
    # Текста нет или сообщение старого образца — хотя бы снимем клавиатуру под ним.
    with contextlib.suppress(TelegramBadRequest):
        await bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=None
        )


async def strip_buttons(
    bot: Bot,
    chat_id: int,
    message_id: int,
    *,
    body: str | None = None,
) -> None:
    """Снять кнопки. У rich-сообщения для этого нужен его текст без кнопок (`body`)."""
    await close_message(bot, chat_id, message_id, body=body)


async def hide_reply_keyboard(event: TelegramObject) -> None:
    """Убрать клавиатуру под полем ввода («Отправить телефон», старое меню), не оставляя следов.

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
    await _delete_tracked(event, state, VENUE_MESSAGE_ID)


async def show_helper(
    event: TelegramObject,
    state: FSMContext,
    *,
    text: str,
    keyboard: ReplyKeyboardMarkup | ReplyKeyboardRemove,
) -> None:
    """Сообщение с клавиатурой под полем ввода (телефон, геопозиция). Тоже в одном экземпляре.

    Только эти две кнопки Telegram не умеет ставить внутрь сообщения.
    """
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
