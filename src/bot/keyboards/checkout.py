"""Клавиатуры шагов оформления."""

from __future__ import annotations

from collections.abc import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import (
    ColorCB,
    ColorsDoneCB,
    OrderCB,
    PickupCB,
    PickupPageCB,
    PrefillCB,
    ProductCB,
    SpoonCB,
    StepCB,
    VideoCB,
)
from bot.keyboards.common import back_button, cancel_button, skip_button
from core.models import Color, PickupPoint, Product
from core.money import format_rubles
from core.services.delivery import PICKUP_POINTS_PAGE_SIZE
from core.text import spoons_phrase, truncate


def products(items: Sequence[tuple[Product, int]]) -> InlineKeyboardMarkup:
    """Боксы с минимальной ценой на кнопке."""
    builder = InlineKeyboardBuilder()
    for product, min_price in items:
        builder.button(
            text=f"{product.name} — от {format_rubles(min_price)}",
            callback_data=ProductCB(product_id=product.id).pack(),
        )
    builder.adjust(1)
    builder.row(cancel_button())
    return builder.as_markup()


def spoons(options: Sequence[tuple[int, int]]) -> InlineKeyboardMarkup:
    """Количество ложечек с ценой: пары (сколько ложечек, цена в копейках)."""
    builder = InlineKeyboardBuilder()
    for count, price in options:
        builder.button(
            text=f"{spoons_phrase(count)} — {format_rubles(price)}",
            callback_data=SpoonCB(count=count).pack(),
        )
    builder.adjust(1)
    builder.row(back_button("product"))
    return builder.as_markup()


def video(price_kopecks: int, *, chosen: bool | None = None) -> InlineKeyboardMarkup:
    """Видео сборки — отдельный вопрос, где «нет» выбирается так же явно, как «да»."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=("✓ " if chosen is True else "") + f"Да, хочу видео (+{format_rubles(price_kopecks)})",
        callback_data=VideoCB(enabled=True).pack(),
    )
    builder.button(
        text=("✓ " if chosen is False else "") + "Без видео",
        callback_data=VideoCB(enabled=False).pack(),
    )
    builder.adjust(1)
    builder.row(back_button("spoons"))
    return builder.as_markup()


def colors(
    palette: Sequence[Color],
    selected: Sequence[int],
    *,
    kind: str,
    back_step: str,
    skippable: bool = False,
) -> InlineKeyboardMarkup:
    """Мультивыбор цветов: отмеченные — с галочкой."""
    builder = InlineKeyboardBuilder()
    chosen = set(selected)
    for color in palette:
        builder.button(
            text=("✓ " if color.id in chosen else "") + color.name,
            callback_data=ColorCB(kind=kind, color_id=color.id).pack(),
        )
    builder.adjust(2)

    navigation = [back_button(back_step)]
    if skippable:
        navigation.append(skip_button(f"colors_{kind}"))
    builder.row(*navigation)
    builder.row(
        InlineKeyboardButton(text="Готово", callback_data=ColorsDoneCB(kind=kind).pack()),
    )
    return builder.as_markup()


def comment(back_step: str = "colors_a") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(back_button(back_step), skip_button("comment"))
    return builder.as_markup()


def pickup_search() -> InlineKeyboardMarkup:
    """Как искать пункт выдачи: геопозиция запрашивается reply-клавиатурой."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="⌨️ Ввести город или адрес",
        callback_data=StepCB(step="pickup_city").pack(),
    )
    builder.adjust(1)
    builder.row(back_button("comment"))
    return builder.as_markup()


def pickup_points(
    points: Sequence[PickupPoint],
    *,
    offset: int,
    has_more: bool,
) -> InlineKeyboardMarkup:
    """Список пунктов: на кнопке короткий адрес, служба названа в тексте сообщения."""
    builder = InlineKeyboardBuilder()
    for point in points:
        builder.button(
            text=truncate(point.address, 60),
            callback_data=PickupCB(point_id=point.id).pack(),
        )
    builder.adjust(1)

    navigation: list[InlineKeyboardButton] = []
    if offset > 0:
        navigation.append(
            InlineKeyboardButton(
                text="← Предыдущие",
                callback_data=PickupPageCB(
                    offset=max(0, offset - PICKUP_POINTS_PAGE_SIZE),
                ).pack(),
            ),
        )
    if has_more:
        navigation.append(
            InlineKeyboardButton(
                text="Ещё →",
                callback_data=PickupPageCB(offset=offset + PICKUP_POINTS_PAGE_SIZE).pack(),
            ),
        )
    if navigation:
        builder.row(*navigation)

    builder.row(back_button("pickup_search"))
    return builder.as_markup()


def prefill(*, has_previous: bool) -> InlineKeyboardMarkup:
    """Предложение подставить данные прошлого заказа."""
    builder = InlineKeyboardBuilder()
    if has_previous:
        builder.button(
            text="Использовать прошлые данные",
            callback_data=PrefillCB(use=True).pack(),
        )
    builder.button(text="Ввести заново", callback_data=PrefillCB(use=False).pack())
    builder.adjust(1)
    return builder.as_markup()


def email_step() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(skip_button("email", "Без email"))
    return builder.as_markup()


def summary() -> InlineKeyboardMarkup:
    """Итог заказа: правки по разделам и переход к оплате."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Изменить бокс", callback_data=StepCB(step="product").pack())
    builder.button(text="✏️ Изменить пожелания", callback_data=StepCB(step="colors_f").pack())
    builder.button(text="✏️ Изменить доставку", callback_data=StepCB(step="pickup_search").pack())
    builder.button(text="✏️ Изменить контакты", callback_data=StepCB(step="recipient").pack())
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(text="💳 Оплатить", callback_data=StepCB(step="pay").pack()),
    )
    builder.row(cancel_button())
    return builder.as_markup()


def payment(pay_url: str, order_id: int) -> InlineKeyboardMarkup:
    """Оформленный заказ: оплатить или отказаться.

    Менять состав уже нельзя — заказ принят и ждёт оплаты.
    """
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="💳 Оплатить", url=pay_url))
    builder.row(
        InlineKeyboardButton(
            text="Отменить заказ",
            callback_data=OrderCB(order_id=order_id, action="cancel").pack(),
        ),
    )
    return builder.as_markup()
