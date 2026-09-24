"""Рабочий режим бота: то, что видит владелица.

Её личная переписка с ботом — это уведомления о заказах и работа с фотографиями
боксов. Клиентский сценарий для этого аккаунта закрыт: заказы оформляют покупатели,
а «заказ самой себе» только путал бы очередь и статистику.

Фото бокса, присланное боту, сразу даёт file_id — идентификатор картинки внутри
Telegram. Мы храним только его, поэтому фото показывается клиентам мгновенно
и не занимает место на сервере.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.callbacks import OwnerPhotoCB
from bot.filters import IsStaff
from bot.keyboards import owner as kb
from bot.keyboards.common import MENU_BUTTONS
from bot.states import PHOTO_PRODUCT_ID, OwnerPhoto
from core.config import get_settings
from core.services import catalog

logger = logging.getLogger(__name__)

#: Все сообщения этого роутера — только от владелицы и из служебного чата.
router = Router(name="owner")
router.message.filter(IsStaff())
router.callback_query.filter(IsStaff())

GREETING = (
    "Рабочий режим 💗\n\n"
    "Сюда приходят уведомления о новых заказах. Здесь же можно обновить фотографии "
    "боксов — они показываются клиентам при выборе.\n\n"
    "Заказы оформляются с аккаунта покупателя, поэтому клиентского меню тут нет."
)


@router.message(CommandStart())
@router.message(Command("menu"))
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(GREETING, reply_markup=kb.owner_menu())


@router.message(Command("id"))
@router.message(F.text == kb.BTN_MY_ID)
async def show_id(message: Message) -> None:
    """Свой id — чтобы вписать его в OWNER_CHAT_ID, не ища сторонних ботов."""
    await message.answer(
        f"id этого чата: <code>{message.chat.id}</code>",
        reply_markup=kb.owner_menu(),
    )


@router.message(F.text == kb.BTN_PANEL)
async def show_panel(message: Message) -> None:
    url = f"{get_settings().admin_base_url}/admin/board"
    keyboard = kb.panel_link(url)
    if keyboard is None:
        # Локальный адрес: Telegram такую ссылку в кнопке не примет.
        await message.answer(f"Панель заказов:\n<code>{url}</code>", reply_markup=kb.owner_menu())
        return
    await message.answer("Панель заказов:", reply_markup=keyboard)


# --- фото боксов ---


@router.message(F.text == kb.BTN_PHOTO)
async def choose_product(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    products = await catalog.list_products_for_admin(session)
    if not products:
        await message.answer(
            "В каталоге пока нет боксов — заведите их в панели.",
            reply_markup=kb.owner_menu(),
        )
        return
    await message.answer(
        "Для какого бокса фото?\n🖼 — фото уже есть, ▫️ — пока нет.",
        reply_markup=kb.products(products),
    )


@router.callback_query(OwnerPhotoCB.filter(F.action == "list"))
async def back_to_list(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await state.clear()
    await callback.answer()
    products = await catalog.list_products_for_admin(session)
    if callback.message is not None:
        await callback.message.edit_text(
            "Для какого бокса фото?\n🖼 — фото уже есть, ▫️ — пока нет.",
            reply_markup=kb.products(products),
        )


@router.callback_query(OwnerPhotoCB.filter(F.action == "pick"))
async def pick_product(
    callback: CallbackQuery,
    callback_data: OwnerPhotoCB,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    product = await catalog.get_product(session, callback_data.product_id)
    await callback.answer()
    await state.set_state(OwnerPhoto.waiting)
    await state.update_data({PHOTO_PRODUCT_ID: product.id})

    if callback.message is None:
        return
    current = "Сейчас фото есть — пришлите новое, чтобы заменить." if product.photo_file_id else ""
    await callback.message.edit_text(
        f"<b>{product.name}</b>\n\nПришлите фотографию сообщением.\n{current}".strip(),
        reply_markup=kb.photo_actions(product.id, has_photo=bool(product.photo_file_id)),
    )
    if product.photo_file_id:
        await callback.message.answer_photo(product.photo_file_id, caption="Текущее фото")


@router.message(OwnerPhoto.waiting, F.photo)
async def receive_photo(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """Сохранить присланное фото за боксом."""
    data = await state.get_data()
    product_id = data.get(PHOTO_PRODUCT_ID)
    if product_id is None:
        await message.answer("Сначала выберите бокс.", reply_markup=kb.owner_menu())
        return

    product = await catalog.get_product(session, product_id)
    # Самый крупный размер: Telegram отдаёт несколько вариантов одной картинки.
    product.photo_file_id = message.photo[-1].file_id
    await session.flush()
    await state.clear()
    await message.answer(
        f"Готово: фото для «{product.name}» обновлено. Клиенты увидят его при выборе бокса.",
        reply_markup=kb.owner_menu(),
    )


@router.message(OwnerPhoto.waiting, F.document)
async def reject_document(message: Message) -> None:
    """Картинка файлом не подойдёт: нужен file_id именно фотографии."""
    await message.answer(
        "Отправьте картинку как фото, а не файлом: в меню вложения выберите «Фото».",
    )


@router.callback_query(OwnerPhotoCB.filter(F.action == "clear"))
async def clear_photo(
    callback: CallbackQuery,
    callback_data: OwnerPhotoCB,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    product = await catalog.get_product(session, callback_data.product_id)
    product.photo_file_id = None
    await session.flush()
    await state.clear()
    await callback.answer("Фото убрано")
    if callback.message is not None:
        await callback.message.edit_text(
            f"<b>{product.name}</b>\n\nФото убрано — бот покажет бокс без картинки.",
            reply_markup=kb.photo_actions(product.id, has_photo=False),
        )


# --- клиентское меню для этого аккаунта закрыто ---


@router.message(F.text.in_(MENU_BUTTONS))
async def client_menu_is_closed(message: Message) -> None:
    await message.answer(
        "Это рабочий аккаунт: заказы оформляются с аккаунта покупателя.\n"
        "Чтобы пройти заказ самой, откройте бота с другого аккаунта.",
        reply_markup=kb.owner_menu(),
    )
