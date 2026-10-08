"""Клавиатуры шагов оформления.

Все кнопки — внутри сообщения сценария: шаг сменяется правкой того же сообщения.
Цвет кнопки (Bot API 9.4+) подсказывает главное действие: синяя — дальше,
зелёная — оплата, красная — отмена.
"""

from __future__ import annotations

from collections.abc import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import (
    CartCB,
    OrderCB,
    PickupCB,
    PickupPageCB,
    PrefillCB,
    ProductCB,
    SpoonCB,
    StepCB,
    VideoCB,
)
from bot.emoji import provider_plain
from bot.keyboards.common import (
    DANGER,
    PRIMARY,
    SUCCESS,
    back_button,
    button,
    cancel_button,
    skip_button,
)
from core.models import OrderItem, PickupPoint, Product
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


def _mark(text: str, chosen: bool) -> str:
    """Выбранный вариант — зелёный и с галочкой, как переключатель."""
    return f"[✓] {text}" if chosen else text


def spoons(options: Sequence[tuple[int, int]], *, chosen: int | None) -> InlineKeyboardMarkup:
    """Количество ложечек с ценой: пары (сколько ложечек, цена в копейках)."""
    builder = InlineKeyboardBuilder()
    for count, price in options:
        builder.row(
            button(
                _mark(f"{spoons_phrase(count)} — {format_rubles(price)}", count == chosen),
                SpoonCB(count=count).pack(),
                style=SUCCESS if count == chosen else None,
            ),
        )
    builder.row(back_button("product"))
    return builder.as_markup()


def video(price_kopecks: int, *, chosen: bool | None = None) -> InlineKeyboardMarkup:
    """Видео сборки — «нет» выбирается так же явно, как «да»."""
    builder = InlineKeyboardBuilder()
    builder.row(
        button(
            _mark(f"🎥 С видео +{format_rubles(price_kopecks)}", chosen is True),
            VideoCB(enabled=True).pack(),
            style=SUCCESS if chosen is True else None,
        ),
    )
    builder.row(
        button(
            _mark("Без видео", chosen is False),
            VideoCB(enabled=False).pack(),
            style=SUCCESS if chosen is False else None,
        ),
    )
    builder.row(back_button("spoons"))
    return builder.as_markup()


def comment(*, has_current: bool, back_step: str) -> InlineKeyboardMarkup:
    """Цвета и пожелания текстом. При правке бокса «пропустить» — оставить как было."""
    builder = InlineKeyboardBuilder()
    skip_text = "Оставить как есть" if has_current else "Без пожеланий"
    builder.row(back_button(back_step), skip_button("comment", skip_text))
    return builder.as_markup()


def cart(items: Sequence[OrderItem], *, can_add: bool) -> InlineKeyboardMarkup:
    """Корзина: править и убирать боксы, добавить ещё один или идти дальше."""
    builder = InlineKeyboardBuilder()
    many = len(items) > 1
    for index, item in enumerate(items, start=1):
        label = f" {index}" if many else ""
        builder.row(
            InlineKeyboardButton(
                text=f"✏️ Бокс{label}",
                callback_data=CartCB(action="edit", item_id=item.id).pack(),
            ),
            InlineKeyboardButton(
                text="🗑",
                callback_data=CartCB(action="remove", item_id=item.id).pack(),
            ),
        )
    if can_add:
        builder.row(button("➕ Ещё бокс", CartCB(action="add").pack()))
    builder.row(button("Далее: доставка →", CartCB(action="next").pack(), style=PRIMARY))
    builder.row(cancel_button())
    return builder.as_markup()


def pickup_search(*, back_step: str = "cart") -> InlineKeyboardMarkup:
    """Шаг поиска пункта: геопозиция — кнопкой под полем ввода, адрес — текстом."""
    builder = InlineKeyboardBuilder()
    builder.row(back_button(back_step))
    return builder.as_markup()


def pickup_points(
    points: Sequence[PickupPoint],
    *,
    offset: int,
    has_more: bool,
) -> InlineKeyboardMarkup:
    """Список пунктов: эмодзи службы и короткий адрес на кнопке."""
    builder = InlineKeyboardBuilder()
    for point in points:
        builder.row(
            button(
                f"{provider_plain(point.provider)} {truncate(point.address, 58)}",
                PickupCB(point_id=point.id).pack(),
            ),
        )

    navigation: list[InlineKeyboardButton] = []
    if offset > 0:
        navigation.append(
            button("←", PickupPageCB(offset=max(0, offset - PICKUP_POINTS_PAGE_SIZE)).pack()),
        )
    if has_more:
        navigation.append(
            button("Ещё →", PickupPageCB(offset=offset + PICKUP_POINTS_PAGE_SIZE).pack())
        )
    if navigation:
        builder.row(*navigation)

    builder.row(back_button("pickup_search"))
    return builder.as_markup()


def confirm_prefilled(what: str, *, keep: str, change: str) -> InlineKeyboardMarkup:
    """Подставленные из прошлого заказа данные: оставить или поменять."""
    builder = InlineKeyboardBuilder()
    builder.row(button(keep, PrefillCB(what=what, use=True).pack(), style=PRIMARY))
    builder.row(button(change, PrefillCB(what=what, use=False).pack()))
    return builder.as_markup()


def resume_draft() -> InlineKeyboardMarkup:
    """Незаконченное оформление: продолжить или начать заново."""
    return confirm_prefilled("draft", keep="Продолжить", change="Начать заново")


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
    builder.row(button("💳 Перейти к оплате", StepCB(step="pay").pack(), style=SUCCESS))
    builder.row(
        button("✏️ Боксы", StepCB(step="cart").pack()),
        button("✏️ Доставка", StepCB(step="pickup_search").pack()),
        button("✏️ Получатель", StepCB(step="recipient").pack()),
    )
    builder.row(cancel_button())
    return builder.as_markup()


def payment(pay_url: str | None, order_id: int) -> InlineKeyboardMarkup:
    """Оформленный заказ: оплатить или отказаться.

    Кнопки снимутся сами, когда заказ оплатят или отменят. `pay_url` пустой —
    ссылка показана в тексте (так бывает на машине разработчика).
    """
    builder = InlineKeyboardBuilder()
    if pay_url:
        builder.row(InlineKeyboardButton(text="💳 Оплатить", url=pay_url, style=SUCCESS))
    builder.row(
        InlineKeyboardButton(
            text="Отменить заказ",
            callback_data=OrderCB(order_id=order_id, action="cancel").pack(),
            style=DANGER,
        ),
    )
    return builder.as_markup()
