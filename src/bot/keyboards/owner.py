"""Клавиатуры рабочего режима: то, что видит владелица."""

from __future__ import annotations

from collections.abc import Sequence

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import OwnerPhotoCB
from core.models import Product
from core.text import truncate

BTN_PHOTO = "📸 Фото бокса"
BTN_PANEL = "🖥 Панель заказов"
BTN_MY_ID = "🆔 Мой id"

#: Кнопки рабочего меню — по ним же ловятся нажатия.
OWNER_BUTTONS = frozenset({BTN_PHOTO, BTN_PANEL, BTN_MY_ID})


def owner_menu() -> ReplyKeyboardMarkup:
    """Постоянная клавиатура владелицы. Кнопки «Заказать бокс» здесь нет."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_PHOTO)],
            [KeyboardButton(text=BTN_PANEL), KeyboardButton(text=BTN_MY_ID)],
        ],
        resize_keyboard=True,
    )


def products(items: Sequence[Product]) -> InlineKeyboardMarkup:
    """Боксы для выбора: видно, у каких фото уже есть."""
    builder = InlineKeyboardBuilder()
    for product in items:
        mark = "🖼" if product.photo_file_id else "▫️"
        builder.button(
            text=f"{mark} {truncate(product.name, 40)}",
            callback_data=OwnerPhotoCB(action="pick", product_id=product.id).pack(),
        )
    builder.adjust(1)
    return builder.as_markup()


def photo_actions(product_id: int, *, has_photo: bool) -> InlineKeyboardMarkup:
    """Что можно сделать с фото выбранного бокса."""
    builder = InlineKeyboardBuilder()
    if has_photo:
        builder.button(
            text="🗑 Убрать фото",
            callback_data=OwnerPhotoCB(action="clear", product_id=product_id).pack(),
        )
    builder.button(
        text="← К списку боксов",
        callback_data=OwnerPhotoCB(action="list", product_id=0).pack(),
    )
    builder.adjust(1)
    return builder.as_markup()


def panel_link(url: str) -> InlineKeyboardMarkup | None:
    """Кнопка-ссылка на панель. Локальный адрес Telegram в кнопке не примет."""
    from core.services.payments import is_button_url

    if not is_button_url(url):
        return None
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🖥 Открыть панель", url=url))
    return builder.as_markup()
