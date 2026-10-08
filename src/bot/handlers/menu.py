"""Главное меню и справочные разделы: цены, доставка, вопросы, контакты.

Меню живёт в одном сообщении: раздел открывается на месте меню, «← Меню»
возвращает обратно.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot import ui
from bot.callbacks import MenuCB
from bot.emoji import provider_icon
from bot.keyboards.common import (
    BTN_CONTACTS,
    BTN_DELIVERY,
    BTN_FAQ,
    BTN_PRICES,
    back_to_menu,
    main_menu,
    menu_button,
    request_location,
)
from core.enums import AddonChargeMode
from core.money import format_rubles
from core.services import catalog, delivery, settings
from core.services.catalog import active_variants
from core.text import spoons_phrase

router = Router(name="menu")

Event = Message | CallbackQuery


@router.message(CommandStart(deep_link=True))
async def start_with_payload(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Возврат из оплаты: t.me/бот?start=paid_<токен>."""
    if (command.args or "").startswith("paid_"):
        await ui.reply(
            message,
            text="Спасибо! Как только оплата подтвердится, пришлём номер заказа 💗",
            keyboard=main_menu(),
        )
        return
    await start(message, state, session)


@router.message(CommandStart())
@router.message(Command("menu"))
async def start(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await _leave_checkout(message, state)
    # Убираем клавиатуру под полем ввода, если она осталась от прошлой версии бота.
    await ui.hide_reply_keyboard(message)
    texts = await settings.get_bot_texts(session)
    await ui.reply(message, text=texts.greeting, keyboard=main_menu())


@router.callback_query(MenuCB.filter(F.section == "home"))
async def home(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await callback.answer()
    await ui.clear_helper(callback, state)
    texts = await settings.get_bot_texts(session)
    await ui.reply(callback, text=texts.greeting, keyboard=main_menu())


async def _leave_checkout(message: Message, state: FSMContext) -> None:
    """Выйти из оформления: у его сообщения снимаются кнопки, черновик остаётся в базе."""
    await ui.retire(message, state)
    await ui.clear_helper(message, state)
    await state.clear()


# --- цены ---


@router.callback_query(MenuCB.filter(F.section == "prices"))
async def prices_button(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    await show_prices(callback, session)


@router.message(F.text == BTN_PRICES)
async def show_prices(event: Event, session: AsyncSession) -> None:
    """Реальные цены из каталога, а не «уточняйте при заказе»."""
    products = await catalog.list_active_products(session)
    if not products:
        await ui.reply(
            event, text="Каталог обновляется — загляните чуть позже.", keyboard=back_to_menu()
        )
        return

    blocks: list[str] = ["<b>💰 Цены</b>"]
    for product in products:
        lines = [f"\n<b>{product.name}</b>"]
        for variant in active_variants(product.variants):
            lines.append(
                f"{spoons_phrase(variant.spoon_count)} — {format_rubles(variant.price_kopecks)}"
            )
        if product.allows_extra_spoons and product.extra_spoon_price_kopecks is not None:
            lines.append(f"+1 ложечка — {format_rubles(product.extra_spoon_price_kopecks)}")
        blocks.append("\n".join(lines))

    addons = await catalog.list_active_addons(session)
    for addon in addons:
        suffix = "за бокс" if addon.charge_mode == AddonChargeMode.PER_BOX else "за заказ"
        blocks.append(f"\n{addon.name} — +{format_rubles(addon.price_kopecks)} {suffix}")

    fixed = await delivery.fixed_price_kopecks(session)
    if fixed is not None:
        blocks.append(f"Доставка — {format_rubles(fixed)}")

    await ui.reply(event, text="\n".join(blocks), keyboard=_order_or_back())


def _order_or_back():
    builder = InlineKeyboardBuilder()
    builder.row(menu_button("🛍 Заказать бокс", "order", style="primary"))
    builder.row(menu_button("← Меню", "home", style="link"))
    return builder.as_markup()


# --- доставка ---


@router.callback_query(MenuCB.filter(F.section == "delivery"))
async def delivery_button(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    await show_delivery(callback, session)


@router.message(F.text == BTN_DELIVERY)
async def show_delivery(event: Event, session: AsyncSession) -> None:
    providers = await delivery.enabled_providers(session)
    lines = ["<b>🚚 Доставка</b>", ""]
    if providers:
        lines.extend(f"{provider_icon(provider.code)} {provider.name}" for provider in providers)
    else:
        lines.append("Пункты выдачи СДЭК и Яндекса")
    fixed = await delivery.fixed_price_kopecks(session)
    if fixed is not None:
        lines.extend(["", f"Стоимость — {format_rubles(fixed)}, фиксированная, входит в чек."])

    builder = InlineKeyboardBuilder()
    builder.row(menu_button("📍 Ближайшие пункты", "nearest"))
    builder.row(menu_button("← Меню", "home", style="link"))
    await ui.reply(event, text="\n".join(lines), keyboard=builder.as_markup())


@router.callback_query(MenuCB.filter(F.section == "nearest"))
async def ask_location(callback: CallbackQuery, state: FSMContext) -> None:
    """Геопозицию Telegram отдаёт только кнопкой под полем ввода — показываем её."""
    await callback.answer()
    await ui.show_helper(
        callback, state, text="Отправьте геопозицию 👇", keyboard=request_location()
    )


# --- вопросы и контакты ---


@router.callback_query(MenuCB.filter(F.section == "faq"))
async def faq_button(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    await show_faq(callback, session)


@router.message(F.text == BTN_FAQ)
async def show_faq(event: Event, session: AsyncSession) -> None:
    faq = await settings.get_faq(session)
    if not faq.items:
        await ui.reply(
            event, text="Напишите нам — ответим на любой вопрос.", keyboard=back_to_menu()
        )
        return
    blocks = ["<b>❓ Частые вопросы</b>"]
    blocks.extend(f"\n<b>{item.question}</b>\n{item.answer}" for item in faq.items)
    await ui.reply(event, text="\n".join(blocks), keyboard=back_to_menu())


@router.callback_query(MenuCB.filter(F.section == "contacts"))
async def contacts_button(callback: CallbackQuery, session: AsyncSession) -> None:
    await callback.answer()
    await show_contacts(callback, session)


@router.message(F.text == BTN_CONTACTS)
async def show_contacts(event: Event, session: AsyncSession) -> None:
    contacts = await settings.get_contacts(session)
    lines = ["<b>📞 Связаться с нами</b>", ""]
    for label, value in (
        ("Telegram", contacts.telegram),
        ("Телефон", contacts.phone),
        ("Почта", contacts.email),
        ("Instagram", contacts.instagram),
        ("TikTok", contacts.tiktok),
    ):
        if value:
            lines.append(f"{label}: {value}")
    if len(lines) == 2:
        lines.append("Напишите нам в личные сообщения.")
    await ui.reply(event, text="\n".join(lines), keyboard=back_to_menu())
