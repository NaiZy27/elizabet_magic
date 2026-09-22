"""Клавиатуры шагов оформления."""

from __future__ import annotations

from collections.abc import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import (
    CartCB,
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
from core.models import Color, OrderItem, PickupPoint, Product
from core.money import format_rubles
from core.services.delivery import PICKUP_POINTS_PAGE_SIZE
from core.text import spoons_phrase, truncate


def products(items: Sequence[tuple[Product, int]], *, back_to_cart: bool) -> InlineKeyboardMarkup:
    """Боксы с минимальной ценой на кнопке."""
    builder = InlineKeyboardBuilder()
    for product, min_price in items:
        builder.button(
            text=f"{product.name} — от {format_rubles(min_price)}",
            callback_data=ProductCB(product_id=product.id).pack(),
        )
    builder.adjust(1)
    if back_to_cart:
        builder.row(back_button("cart"))
    builder.row(cancel_button())
    return builder.as_markup()


def spoons(options: Sequence[tuple[int, int]], *, chosen: int | None) -> InlineKeyboardMarkup:
    """Количество ложечек с ценой: пары (сколько ложечек, цена в копейках)."""
    builder = InlineKeyboardBuilder()
    for count, price in options:
        builder.button(
            text=("✓ " if count == chosen else "")
            + f"{spoons_phrase(count)} — {format_rubles(price)}",
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

    builder.row(back_button(back_step), skip_button(f"colors_{kind}", "Не важно"))
    builder.row(
        InlineKeyboardButton(text="Готово →", callback_data=ColorsDoneCB(kind=kind).pack()),
    )
    return builder.as_markup()


def comment(*, has_current: bool) -> InlineKeyboardMarkup:
    """Пожелания. При правке бокса «пропустить» означает «оставить как было»."""
    builder = InlineKeyboardBuilder()
    skip_text = "Оставить как есть" if has_current else "Без пожеланий"
    builder.row(back_button("colors_a"), skip_button("comment", skip_text))
    return builder.as_markup()


def cart(items: Sequence[OrderItem], *, can_add: bool) -> InlineKeyboardMarkup:
    """Корзина: править и убирать боксы, добавить ещё один или идти дальше."""
    builder = InlineKeyboardBuilder()
    many = len(items) > 1
    for index, item in enumerate(items, start=1):
        label = f"бокс {index}" if many else "бокс"
        builder.row(
            InlineKeyboardButton(
                text=f"✏️ Изменить {label}",
                callback_data=CartCB(action="edit", item_id=item.id).pack(),
            ),
            InlineKeyboardButton(
                text="🗑 Убрать",
                callback_data=CartCB(action="remove", item_id=item.id).pack(),
            ),
        )
    if can_add:
        builder.row(
            InlineKeyboardButton(
                text="➕ Добавить ещё бокс",
                callback_data=CartCB(action="add").pack(),
            ),
        )
    builder.row(
        InlineKeyboardButton(text="Далее: доставка →", callback_data=CartCB(action="next").pack()),
    )
    builder.row(cancel_button())
    return builder.as_markup()


def pickup_search(*, back_step: str = "cart") -> InlineKeyboardMarkup:
    """Как искать пункт выдачи: геопозиция запрашивается reply-клавиатурой."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="⌨️ Ввести город или адрес",
        callback_data=StepCB(step="pickup_city").pack(),
    )
    builder.adjust(1)
    builder.row(back_button(back_step))
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


def confirm_prefilled(what: str, *, keep: str, change: str) -> InlineKeyboardMarkup:
    """Подставленные из прошлого заказа данные: оставить или поменять."""
    builder = InlineKeyboardBuilder()
    builder.button(text=keep, callback_data=PrefillCB(what=what, use=True).pack())
    builder.button(text=change, callback_data=PrefillCB(what=what, use=False).pack())
    builder.adjust(1)
    return builder.as_markup()


def resume_draft() -> InlineKeyboardMarkup:
    """Незаконченное оформление: продолжить или начать заново."""
    return confirm_prefilled("draft", keep="Продолжить оформление", change="Начать заново")


def email_step(*, has_current: bool) -> InlineKeyboardMarkup | None:
    """Email обязателен, поэтому «пропустить» нельзя — только оставить уже указанный."""
    if not has_current:
        return None
    builder = InlineKeyboardBuilder()
    builder.row(skip_button("email", "Оставить этот email"))
    return builder.as_markup()


def summary() -> InlineKeyboardMarkup:
    """Итог заказа: правки по разделам и переход к оплате."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Боксы и пожелания", callback_data=StepCB(step="cart").pack())
    builder.button(text="✏️ Доставка", callback_data=StepCB(step="pickup_search").pack())
    builder.button(text="✏️ Получатель", callback_data=StepCB(step="recipient").pack())
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(text="💳 Перейти к оплате", callback_data=StepCB(step="pay").pack()),
    )
    builder.row(cancel_button())
    return builder.as_markup()


def payment(pay_url: str | None, order_id: int) -> InlineKeyboardMarkup:
    """Оформленный заказ: оплатить или отказаться.

    Менять состав уже нельзя — заказ ждёт оплаты. Кнопки снимутся сами, когда заказ
    оплатят или отменят. `pay_url` пустой — ссылка показана в тексте сообщения
    (так бывает на машине разработчика, где адрес виден только локально).
    """
    builder = InlineKeyboardBuilder()
    if pay_url:
        builder.row(InlineKeyboardButton(text="💳 Оплатить", url=pay_url))
    builder.row(
        InlineKeyboardButton(
            text="Отменить заказ",
            callback_data=OrderCB(order_id=order_id, action="cancel").pack(),
        ),
    )
    return builder.as_markup()
