"""Рабочий режим бота: то, что видит владелица.

Её личная переписка с ботом — это уведомления о заказах и работа с фотографиями
боксов. Клиентский сценарий для этого аккаунта закрыт: заказы оформляют покупатели,
а «заказ самой себе» только путал бы очередь и статистику.

Фото бокса, присланное боту, сразу даёт file_id — идентификатор картинки внутри
Telegram. Мы храним только его, поэтому фото показывается клиентам мгновенно
и не занимает место на сервере.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import ui
from bot.callbacks import MenuCB, OwnerPhotoCB, ReviewAdminCB
from bot.filters import IsStaff
from bot.keyboards import owner as kb
from bot.keyboards.common import MENU_BUTTONS
from bot.states import PHOTO_PRODUCT_ID, OwnerPhoto
from core.clock import format_date
from core.config import get_settings
from core.errors import NotFoundError
from core.services import catalog, reviews

logger = logging.getLogger(__name__)

#: Все сообщения этого роутера — только от владелицы и из служебного чата.
router = Router(name="owner")
router.message.filter(IsStaff())
router.callback_query.filter(IsStaff())

GREETING = "Рабочий режим 💗\nСюда приходят новые заказы и отзывы."


@router.message(CommandStart())
@router.message(Command("menu"))
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await ui.hide_reply_keyboard(message)
    await ui.reply(message, text=GREETING, keyboard=kb.owner_menu())


@router.callback_query(MenuCB.filter(F.section == "o_home"))
async def owner_home(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await ui.reply(callback, text=GREETING, keyboard=kb.owner_menu())


@router.callback_query(MenuCB.filter(F.section == "o_photo"))
async def photo_button(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await callback.answer()
    await choose_product(callback, state, session)


# --- отзывы ---


@router.callback_query(MenuCB.filter(F.section == "o_reviews"))
async def reviews_button(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    await _show_review(callback, session, 0)


@router.callback_query(ReviewAdminCB.filter(F.action == "page"))
async def review_page(
    callback: CallbackQuery,
    callback_data: ReviewAdminCB,
    session: AsyncSession,
) -> None:
    await callback.answer()
    await _show_review(callback, session, callback_data.index)


async def _show_review(callback: CallbackQuery, session: AsyncSession, index: int) -> None:
    total = await reviews.count_visible(session)
    if total == 0:
        await ui.reply(callback, text="Отзывов пока нет.", keyboard=kb.owner_menu())
        return
    index = min(max(0, index), total - 1)
    review = await reviews.visible_at(session, index)
    if review is None:
        return
    lines = [f"⭐ <b>Отзыв</b> · {format_date(review.created_at.date())}", ""]
    if review.text:
        lines.append(f"«{review.text}»")
    lines.append(f"— {reviews.author(review)}")
    if review.photo_file_id:
        lines.append("🖼 с фото")
    await ui.reply(
        callback,
        text="\n".join(lines),
        keyboard=kb.review_browser(
            index=index,
            total=total,
            review_id=review.id,
            has_photo=bool(review.photo_file_id),
            posted=review.posted_at is not None,
        ),
    )


@router.callback_query(ReviewAdminCB.filter(F.action == "photo"))
async def review_photo(
    callback: CallbackQuery,
    callback_data: ReviewAdminCB,
    session: AsyncSession,
) -> None:
    try:
        review = await reviews.get(session, callback_data.review_id)
    except NotFoundError as error:
        await callback.answer(error.message, show_alert=True)
        return
    await callback.answer()
    if review.photo_file_id and callback.message is not None:
        await callback.message.answer_photo(review.photo_file_id)


@router.callback_query(ReviewAdminCB.filter(F.action == "hide"))
async def review_hide(
    callback: CallbackQuery,
    callback_data: ReviewAdminCB,
    session: AsyncSession,
) -> None:
    try:
        review = await reviews.get(session, callback_data.review_id)
    except NotFoundError as error:
        await callback.answer(error.message, show_alert=True)
        return
    await reviews.hide(session, review)
    await callback.answer("Отзыв скрыт")
    await _show_review(callback, session, callback_data.index)


@router.callback_query(ReviewAdminCB.filter(F.action == "post"))
async def review_post(
    callback: CallbackQuery,
    callback_data: ReviewAdminCB,
    session: AsyncSession,
) -> None:
    """Опубликовать отзыв в канал от имени бота."""
    channel = get_settings().owner.reviews_channel_id
    if not channel:
        await callback.answer(
            "Канал для отзывов ещё не подключён: добавьте бота в канал администратором "
            "и пришлите ссылку на канал программисту.",
            show_alert=True,
        )
        return
    try:
        review = await reviews.get(session, callback_data.review_id)
    except NotFoundError as error:
        await callback.answer(error.message, show_alert=True)
        return

    chat_id: int | str = int(channel) if channel.lstrip("-").isdigit() else channel
    text = reviews.channel_text(review)
    try:
        if review.photo_file_id:
            await callback.bot.send_photo(chat_id=chat_id, photo=review.photo_file_id, caption=text)
        else:
            await callback.bot.send_message(chat_id=chat_id, text=text)
    except (TelegramBadRequest, TelegramForbiddenError) as error:
        logger.warning("Отзыв %s не ушёл в канал: %s", review.id, error)
        await callback.answer(
            "Не получилось: проверьте, что бот — администратор канала с правом публикации.",
            show_alert=True,
        )
        return
    await reviews.mark_posted(session, review)
    await callback.answer("Опубликовано в канале ✅")
    message = callback.message
    if isinstance(message, Message) and message.text and message.text.startswith("💌"):
        # Уведомление о новом отзыве: отмечаем, что он уже в канале.
        await ui.mark_choice(message, "✅ Опубликован в канале")
    else:
        await _show_review(callback, session, callback_data.index)


@router.message(F.text == kb.BTN_PANEL)
async def show_panel(message: Message) -> None:
    url = f"{get_settings().admin_base_url}/admin/board"
    keyboard = kb.panel_link(url)
    if keyboard is None:
        # Локальный адрес: Telegram такую ссылку в кнопке не примет.
        await ui.reply(
            message, text=f"Панель заказов:\n<code>{url}</code>", keyboard=kb.owner_menu()
        )
        return
    await ui.reply(message, text="Панель заказов:", keyboard=keyboard)


# --- фото боксов ---


@router.message(F.text == kb.BTN_PHOTO)
async def choose_product(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await state.clear()
    products = await catalog.list_products_for_admin(session)
    if not products:
        await ui.reply(event, text="В каталоге пока нет боксов.", keyboard=kb.owner_menu())
        return
    await ui.reply(
        event,
        text="Для какого бокса фото?\n🖼 — есть, ▫️ — нет.",
        keyboard=kb.products(products),
    )


@router.callback_query(OwnerPhotoCB.filter(F.action == "list"))
async def back_to_list(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await state.clear()
    await callback.answer()
    products = await catalog.list_products_for_admin(session)
    if callback.message is not None:
        await ui.reply(
            callback,
            text="Для какого бокса фото?\n🖼 — есть, ▫️ — нет.",
            keyboard=kb.products(products),
        )


@router.callback_query(OwnerPhotoCB.filter(F.action == "pick"))
async def pick_product(
    callback: CallbackQuery,
    callback_data: OwnerPhotoCB,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    product = await catalog.get_product(session, callback_data.product_id)
    await callback.answer()
    await state.set_state(OwnerPhoto.waiting)
    await state.update_data({PHOTO_PRODUCT_ID: product.id})

    if callback.message is None:
        return
    current = "Сейчас фото есть — пришлите новое, чтобы заменить." if product.photo_file_id else ""
    await ui.reply(
        callback,
        text=f"<b>{product.name}</b>\n\nПришлите фотографию сообщением.\n{current}".strip(),
        keyboard=kb.photo_actions(product.id, has_photo=bool(product.photo_file_id)),
    )
    if product.photo_file_id:
        await callback.message.answer_photo(product.photo_file_id, caption="Текущее фото")


@router.message(OwnerPhoto.waiting, F.photo)
async def receive_photo(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """Сохранить присланное фото за боксом."""
    data = await state.get_data()
    product_id = data.get(PHOTO_PRODUCT_ID)
    if product_id is None:
        await ui.reply(message, text="Сначала выберите бокс.", keyboard=kb.owner_menu())
        return

    product = await catalog.get_product(session, product_id)
    # Самый крупный размер: Telegram отдаёт несколько вариантов одной картинки.
    product.photo_file_id = message.photo[-1].file_id
    await session.flush()
    await state.clear()
    await ui.reply(
        message,
        text=f"Готово: фото для «{product.name}» обновлено.",
        keyboard=kb.owner_menu(),
    )


@router.message(OwnerPhoto.waiting, F.document)
async def reject_document(message: Message) -> None:
    """Картинка файлом не подойдёт: нужен file_id именно фотографии."""
    await message.answer(
        "Отправьте картинку как фото, а не файлом: в меню вложения выберите «Фото».",
    )


@router.callback_query(OwnerPhotoCB.filter(F.action == "clear"))
async def clear_photo(
    callback: CallbackQuery,
    callback_data: OwnerPhotoCB,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    product = await catalog.get_product(session, callback_data.product_id)
    product.photo_file_id = None
    await session.flush()
    await state.clear()
    await callback.answer("Фото убрано")
    if callback.message is not None:
        await ui.reply(
            callback,
            text=f"<b>{product.name}</b>\n\nФото убрано — бот покажет бокс без картинки.",
            keyboard=kb.photo_actions(product.id, has_photo=False),
        )


# --- клиентское меню для этого аккаунта закрыто ---


CLOSED_TEXT = "Это рабочий аккаунт: заказы оформляются с аккаунта покупателя."


@router.message(F.text.in_(MENU_BUTTONS))
async def client_menu_is_closed(message: Message) -> None:
    await ui.reply(message, text=CLOSED_TEXT, keyboard=kb.owner_menu())


@router.callback_query(MenuCB.filter())
async def client_menu_button_is_closed(callback: CallbackQuery) -> None:
    await callback.answer()
    await ui.reply(callback, text=CLOSED_TEXT, keyboard=kb.owner_menu())


@router.callback_query()
async def client_button_is_closed(callback: CallbackQuery) -> None:
    """Любая другая клиентская кнопка — например, из старого сообщения времён тестов.

    Клиентским обработчикам нужна карточка покупателя, а у рабочего аккаунта её нет:
    без этой заглушки нажатие падало бы с ошибкой. Снимаем кнопки и объясняем.
    """
    await callback.answer(CLOSED_TEXT, show_alert=True)
    if isinstance(callback.message, Message):
        # У rich-сообщения кнопки — часть текста: снять их можно, только переписав текст.
        await ui.strip_buttons(
            callback.bot,
            callback.message.chat.id,
            callback.message.message_id,
            body=ui.message_body(callback.message),
        )
