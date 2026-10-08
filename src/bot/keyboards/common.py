"""Главное меню и общие кнопки.

Меню — кнопки внутри сообщения: разделы открываются в том же сообщении, а не
лентой новых. Под полем ввода клавиатура появляется только там, где Telegram
иначе не умеет: отправить геопозицию и телефон контактом.
"""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import MenuCB, SkipCB, StepCB

# Подписи прежней клавиатуры под полем ввода. У кого она осталась с прошлых версий,
# нажатия по-прежнему ловятся, а /start её убирает.
BTN_ORDER = "🛍 Заказать бокс"
BTN_MY_ORDERS = "📦 Мои заказы"
BTN_PRICES = "💰 Цены"
BTN_DELIVERY = "🚚 Доставка и ПВЗ"
BTN_FAQ = "❓ Частые вопросы"
BTN_CONTACTS = "📞 Связаться с нами"

BTN_SEND_PHONE = "📱 Отправить телефон"
BTN_SEND_LOCATION = "📍 Отправить геопозицию"
BTN_CANCEL = "✖️ Отменить оформление"

#: Нажатия кнопок меню — не ответ на шаг оформления: их текстовые шаги пропускают.
MENU_BUTTONS = frozenset(
    {BTN_ORDER, BTN_MY_ORDERS, BTN_PRICES, BTN_DELIVERY, BTN_FAQ, BTN_CONTACTS, BTN_CANCEL},
)

# Стили кнопок (Bot API 9.4+): цвет подсказывает главное действие на экране.
PRIMARY = "primary"
SUCCESS = "success"
DANGER = "danger"


def button(text: str, callback_data: str, *, style: str | None = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=callback_data, style=style)


def menu_button(text: str, section: str, *, style: str | None = None) -> InlineKeyboardButton:
    return button(text, MenuCB(section=section).pack(), style=style)


def main_menu() -> InlineKeyboardMarkup:
    """Главное меню внутри сообщения."""
    builder = InlineKeyboardBuilder()
    builder.row(menu_button("🛍 Заказать бокс", "order", style=PRIMARY))
    builder.row(menu_button("📦 Мои заказы", "orders"), menu_button("💰 Цены", "prices"))
    builder.row(menu_button("🚚 Доставка", "delivery"), menu_button("❓ Вопросы", "faq"))
    builder.row(menu_button("📞 Связаться с нами", "contacts"))
    return builder.as_markup()


#: Второстепенные кнопки («Назад», «Меню») — ссылкой без рамки. Только в rich-сообщениях:
#: для обычной клавиатуры стиль сбрасывается (см. bot.rich.classic_keyboard).
LINK = "link"


def back_to_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(menu_button("← Меню", "home", style=LINK))
    return builder.as_markup()


def request_phone() -> ReplyKeyboardMarkup:
    """Телефон контактом Telegram — одним нажатием и без опечаток."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_SEND_PHONE, request_contact=True, style=PRIMARY)]],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Или напишите номер",
    )


def request_location() -> ReplyKeyboardMarkup:
    """Геопозиция для поиска ближайших пунктов выдачи."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_SEND_LOCATION, request_location=True, style=PRIMARY)]],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Или напишите город и улицу",
    )


def remove_keyboard() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


def back_button(step: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text="← Назад", callback_data=StepCB(step=step).pack(), style=LINK)


def skip_button(step: str, text: str = "Пропустить") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=SkipCB(step=step).pack())


def cancel_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text="✖️ Отменить",
        callback_data=StepCB(step="cancel").pack(),
        style=DANGER,
    )


def single_button(text: str, callback_data: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(button(text, callback_data, style=PRIMARY))
    return builder.as_markup()
