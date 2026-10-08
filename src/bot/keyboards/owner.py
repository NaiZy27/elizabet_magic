"""Клавиатуры рабочего режима: то, что видит владелица."""

from __future__ import annotations

from collections.abc import Sequence

from aiogram.types import DisabledButton, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import MenuCB, OwnerPhotoCB, ReviewAdminCB
from bot.keyboards.common import DANGER, SUCCESS, button, menu_button
from core.config import get_settings
from core.models import Product
from core.text import truncate

BTN_PHOTO = "📸 Фото бокса"
BTN_PANEL = "🖥 Панель заказов"

#: Кнопки рабочего меню — по ним же ловятся нажатия.
OWNER_BUTTONS = frozenset({BTN_PHOTO, BTN_PANEL})


def owner_menu() -> InlineKeyboardMarkup:
    """Меню владелицы внутри сообщения. Кнопки «Заказать бокс» здесь нет."""
    from core.services.payments import is_button_url

    builder = InlineKeyboardBuilder()
    builder.row(menu_button("⭐ Отзывы", "o_reviews"), menu_button(BTN_PHOTO, "o_photo"))
    url = f"{get_settings().admin_base_url}/admin/board"
    if is_button_url(url):
        builder.row(InlineKeyboardButton(text=BTN_PANEL, url=url))
    return builder.as_markup()


def to_owner_menu() -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text="← Меню",
        callback_data=MenuCB(section="o_home").pack(),
        style="link",
    )


def review_browser(
    *,
    index: int,
    total: int,
    review_id: int,
    has_photo: bool,
    posted: bool,
) -> InlineKeyboardMarkup:
    """Листалка отзывов. Счётчик — неактивная кнопка (Bot API 10.3)."""
    builder = InlineKeyboardBuilder()
    builder.row(
        button("◀", ReviewAdminCB(action="page", index=max(0, index - 1)).pack()),
        InlineKeyboardButton(text=f"{index + 1} / {total}", disabled=DisabledButton()),
        button("▶", ReviewAdminCB(action="page", index=min(total - 1, index + 1)).pack()),
    )
    builder.row(
        button(
            "✅ Уже в канале · ещё раз" if posted else "📢 Отправить в канал",
            ReviewAdminCB(action="post", index=index, review_id=review_id).pack(),
            style=None if posted else SUCCESS,
        ),
    )
    extra = []
    if has_photo:
        extra.append(button("🖼 Фото", ReviewAdminCB(action="photo", review_id=review_id).pack()))
    extra.append(
        button(
            "🗑 Скрыть",
            ReviewAdminCB(action="hide", index=index, review_id=review_id).pack(),
            style=DANGER,
        ),
    )
    builder.row(*extra)
    builder.row(to_owner_menu())
    return builder.as_markup()


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
    builder.row(to_owner_menu())
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
