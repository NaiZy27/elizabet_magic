"""Отзывы клиентов.

Клиент нажимает «💌 Оставить отзыв» (в сообщении о получении заказа или в карточке
заказа) и присылает текст, фото с подписью или просто фото. Отзыв сохраняется,
владелице приходит уведомление с кнопкой «📢 В канал».
"""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot import ui
from bot.callbacks import ReviewAdminCB, ReviewCB
from bot.keyboards.common import MENU_BUTTONS, SUCCESS, button, main_menu
from bot.states import REVIEW_ORDER_ID, ReviewForm
from core.config import get_settings
from core.errors import DomainError
from core.models import Customer, Order
from core.services import reviews
from core.text import truncate

logger = logging.getLogger(__name__)

router = Router(name="reviews")


@router.callback_query(ReviewCB.filter(F.action == "new"))
async def ask_review(
    callback: CallbackQuery,
    callback_data: ReviewCB,
    state: FSMContext,
    session: AsyncSession,
    customer: Customer,
) -> None:
    order = await session.get(Order, callback_data.order_id) if callback_data.order_id else None
    order_id = order.id if order is not None and order.customer_id == customer.id else None
    await callback.answer()
    await state.set_state(ReviewForm.waiting)
    await state.update_data({REVIEW_ORDER_ID: order_id})
    builder = InlineKeyboardBuilder()
    builder.button(text="Отмена", callback_data=ReviewCB(action="cancel").pack())
    if callback.message is not None:
        await callback.message.answer(
            "💌 Напишите отзыв одним сообщением — можно с фото.",
            reply_markup=builder.as_markup(),
        )


@router.callback_query(ReviewCB.filter(F.action == "cancel"))
async def cancel_review(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await ui.reply(callback, text="Хорошо, в другой раз 💗", keyboard=main_menu())


@router.message(ReviewForm.waiting, F.photo | (F.text & ~F.text.in_(MENU_BUTTONS)))
async def receive_review(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    customer: Customer,
) -> None:
    data = await state.get_data()
    try:
        review = await reviews.create(
            session,
            customer,
            text=message.caption or message.text or "",
            photo_file_id=message.photo[-1].file_id if message.photo else None,
            order_id=data.get(REVIEW_ORDER_ID),
        )
    except DomainError as error:
        await message.answer(error.message)
        return
    await state.clear()
    await message.answer("Спасибо за отзыв 💗", reply_markup=main_menu())
    await _notify_owner(
        message.bot,
        review.id,
        text=review.text,
        has_photo=bool(review.photo_file_id),
    )


async def _notify_owner(bot: Bot | None, review_id: int, *, text: str, has_photo: bool) -> None:
    chat_id = get_settings().owner.owner_chat_id
    if bot is None or chat_id is None:
        return
    body = f"«{truncate(text, 700)}»" if text else ""
    photo = "\n🖼 с фото" if has_photo else ""
    builder = InlineKeyboardBuilder()
    builder.row(
        button(
            "📢 В канал",
            ReviewAdminCB(action="post", review_id=review_id).pack(),
            style=SUCCESS,
        ),
    )
    builder.row(button("⭐ Все отзывы", ReviewAdminCB(action="page", index=0).pack()))
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=f"💌 <b>Новый отзыв</b>\n\n{body}{photo}",
            reply_markup=builder.as_markup(),
        )
    except (TelegramBadRequest, TelegramForbiddenError) as error:
        logger.warning("Не удалось сообщить владелице об отзыве: %s", error)
