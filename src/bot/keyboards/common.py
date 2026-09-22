"""Главное меню и общие кнопки."""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import SkipCB, StepCB

# Подписи главного меню. По ним же ловятся нажатия, поэтому они собраны здесь.
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


def main_menu() -> ReplyKeyboardMarkup:
    """Постоянная клавиатура под полем ввода."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_ORDER)],
            [KeyboardButton(text=BTN_MY_ORDERS), KeyboardButton(text=BTN_PRICES)],
            [KeyboardButton(text=BTN_DELIVERY), KeyboardButton(text=BTN_FAQ)],
            [KeyboardButton(text=BTN_CONTACTS)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите пункт меню",
    )


def request_phone() -> ReplyKeyboardMarkup:
    """Кнопка, которой клиент отдаёт свой номер одним нажатием."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_SEND_PHONE, request_contact=True)],
            [KeyboardButton(text=BTN_CANCEL)],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Нажмите кнопку или введите номер",
    )


def request_location() -> ReplyKeyboardMarkup:
    """Геопозиция для поиска ближайших пунктов выдачи."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_SEND_LOCATION, request_location=True)],
            [KeyboardButton(text=BTN_CANCEL)],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Или напишите город и улицу",
    )


def remove_keyboard() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


def back_button(step: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text="← Назад", callback_data=StepCB(step=step).pack())


def skip_button(step: str, text: str = "Пропустить") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=SkipCB(step=step).pack())


def cancel_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text="✖️ Отменить оформление",
        callback_data=StepCB(step="cancel").pack(),
    )


def single_button(text: str, callback_data: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=text, callback_data=callback_data)
    return builder.as_markup()
