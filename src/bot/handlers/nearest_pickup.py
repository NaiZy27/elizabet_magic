"""Ближайшие пункты выдачи по геопозиции — вне оформления заказа.

Клиент в любой момент присылает геопозицию (кнопкой из раздела «Доставка и ПВЗ»
или через «📎 → Геопозиция») и получает пять ближайших пунктов СДЭК и Яндекса
с расстоянием, адресом и режимом работы. Внутри оформления геопозицию ловит
шаг доставки в checkout — там клиент сразу выбирает пункт для заказа.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot import ui
from bot.callbacks import NearestPointCB
from bot.emoji import provider_icon, provider_plain
from bot.keyboards.common import main_menu, menu_button
from core.config import get_settings
from core.errors import NotFoundError
from core.labels import delivery_provider_label
from core.models import PickupPoint
from core.services import delivery
from core.text import truncate

router = Router(name="nearest_pickup")

#: Сколько пунктов предлагаем.
NEAREST_COUNT = 5


@router.message(StateFilter(None), F.location)
async def nearest_points(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """Пять ближайших пунктов. Геопозицию клиента не сохраняем."""
    # Кнопка геопозиции под полем ввода больше не нужна.
    await ui.clear_helper(message, state)
    await ui.hide_reply_keyboard(message)
    location = message.location
    found = await delivery.search_nearest(
        session,
        latitude=location.latitude,
        longitude=location.longitude,
        environment=get_settings().delivery_environment,
        limit=NEAREST_COUNT,
    )
    if not found:
        await ui.reply(message, text="Рядом пунктов выдачи не нашли.", keyboard=main_menu())
        return

    lines = ["<b>📍 Ближайшие пункты</b>", ""]
    for point, km in found:
        lines.append(
            f"{provider_icon(point.provider)} <b>{truncate(point.address, 90)}</b>"
            f" · {_format_distance(km)}",
        )
        if point.working_hours:
            lines.append(f"   🕒 {truncate(point.working_hours, 80)}")

    await ui.reply(message, text="\n".join(lines), keyboard=_points_keyboard(found))


@router.callback_query(NearestPointCB.filter())
async def show_point_on_map(
    callback: CallbackQuery,
    callback_data: NearestPointCB,
    session: AsyncSession,
) -> None:
    try:
        point = await delivery.get_pickup_point(session, callback_data.point_id)
    except NotFoundError as error:
        await callback.answer(error.message, show_alert=True)
        return
    await callback.answer()
    if callback.message is not None:
        await callback.message.answer_venue(
            latitude=point.latitude,
            longitude=point.longitude,
            title=f"{delivery_provider_label(point.provider)}: {point.name}"[:256],
            address=point.address,
        )


def _points_keyboard(found: list[tuple[PickupPoint, float]]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for point, _ in found:
        builder.button(
            text=f"{provider_plain(point.provider)} {truncate(point.address, 50)}",
            callback_data=NearestPointCB(point_id=point.id).pack(),
        )
    builder.adjust(1)
    builder.row(menu_button("🛍 Заказать бокс", "order", style="primary"))
    builder.row(menu_button("← Меню", "home", style="link"))
    return builder.as_markup()


def _format_distance(km: float) -> str:
    if km < 1:
        return f"{round(km * 1000 / 10) * 10:.0f} м"
    if km < 10:
        return f"{km:.1f} км".replace(".", ",")
    return f"{km:.0f} км"
