"""Главное меню и справочные разделы: цены, доставка, вопросы, контакты."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards.common import (
    BTN_CONTACTS,
    BTN_DELIVERY,
    BTN_FAQ,
    BTN_PRICES,
    main_menu,
)
from core.enums import AddonChargeMode
from core.models import Customer
from core.money import format_rubles
from core.services import catalog, delivery, settings
from core.services.catalog import active_variants
from core.text import spoons_phrase

router = Router(name="menu")


@router.message(CommandStart(deep_link=True))
async def start_with_payload(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Возврат из оплаты: t.me/бот?start=paid_<токен>."""
    payload = command.args or ""
    texts = await settings.get_bot_texts(session)
    if payload.startswith("paid_"):
        await message.answer(
            "Спасибо! Как только оплата подтвердится, пришлём сообщение о том, "
            "что заказ принят в работу. Обычно это занимает меньше минуты.",
            reply_markup=main_menu(),
        )
        return
    await state.clear()
    await message.answer(texts.greeting, reply_markup=main_menu())


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    texts = await settings.get_bot_texts(session)
    await message.answer(texts.greeting, reply_markup=main_menu())


@router.message(Command("menu"))
async def show_menu(message: Message, state: FSMContext) -> None:
    """Вернуться в меню, прервав незаконченный шаг."""
    await state.clear()
    await message.answer("Главное меню", reply_markup=main_menu())


@router.message(F.text == BTN_PRICES)
async def show_prices(message: Message, session: AsyncSession) -> None:
    """Реальные цены из каталога, а не отсылка «уточняйте при заказе»."""
    products = await catalog.list_active_products(session)
    if not products:
        await message.answer(
            "Каталог сейчас обновляется. Загляните чуть позже или напишите нам.",
            reply_markup=main_menu(),
        )
        return

    blocks: list[str] = ["<b>💰 Цены</b>"]
    for product in products:
        lines = [f"\n<b>{product.name}</b>"]
        for variant in active_variants(product.variants):
            price = format_rubles(variant.price_kopecks)
            lines.append(f"{spoons_phrase(variant.spoon_count)} — {price}")
        if product.allows_extra_spoons and product.extra_spoon_price_kopecks is not None:
            lines.append(
                f"Каждая следующая ложечка — {format_rubles(product.extra_spoon_price_kopecks)}"
                f" (максимум {product.max_spoon_count})",
            )
        blocks.append("\n".join(lines))

    addons = await catalog.list_active_addons(session)
    if addons:
        blocks.append("\n<b>Дополнительно</b>")
        for addon in addons:
            suffix = (
                "за каждый бокс"
                if addon.charge_mode == AddonChargeMode.PER_BOX
                else "за заказ"
            )
            blocks.append(f"{addon.name} — +{format_rubles(addon.price_kopecks)} {suffix}")

    await message.answer("\n".join(blocks), reply_markup=main_menu())


@router.message(F.text == BTN_DELIVERY)
async def show_delivery(message: Message, session: AsyncSession) -> None:
    providers = await delivery.enabled_providers(session)
    if not providers:
        await message.answer(
            "Доставку настраиваем — напишите нам, и мы подберём удобный вариант.",
            reply_markup=main_menu(),
        )
        return

    names = ", ".join(provider.name for provider in providers)
    await message.answer(
        "<b>🚚 Доставка</b>\n\n"
        f"Отправляем в пункты выдачи: {names}.\n"
        "При оформлении заказа можно отправить геопозицию — покажем ближайшие пункты "
        "с адресами и режимом работы, а стоимость посчитаем сразу.\n\n"
        "Собираем заказ, отвозим в службу доставки и присылаем номер для отслеживания.",
        reply_markup=main_menu(),
    )


@router.message(F.text == BTN_FAQ)
async def show_faq(message: Message, session: AsyncSession) -> None:
    faq = await settings.get_faq(session)
    if not faq.items:
        await message.answer(
            "Пока здесь пусто. Напишите нам — ответим на любой вопрос.",
            reply_markup=main_menu(),
        )
        return

    blocks = ["<b>❓ Частые вопросы</b>"]
    for item in faq.items:
        blocks.append(f"\n<b>{item.question}</b>\n{item.answer}")
    await message.answer("\n".join(blocks), reply_markup=main_menu())


@router.message(F.text == BTN_CONTACTS)
async def show_contacts(message: Message, session: AsyncSession) -> None:
    contacts = await settings.get_contacts(session)
    lines = ["<b>📞 Связаться с нами</b>", ""]
    if contacts.telegram:
        lines.append(f"Telegram: {contacts.telegram}")
    if contacts.phone:
        lines.append(f"Телефон: {contacts.phone}")
    if contacts.email:
        lines.append(f"Почта: {contacts.email}")
    if contacts.instagram:
        lines.append(f"Instagram: {contacts.instagram}")
    if contacts.tiktok:
        lines.append(f"TikTok: {contacts.tiktok}")
    if len(lines) == 2:
        lines.append("Напишите нам в личные сообщения — ответим и поможем.")

    await message.answer("\n".join(lines), reply_markup=main_menu())
