"""«Мои заказы»: список, карточка с лентой этапов, чек, оплата и отмена.

Сообщения с кнопками «Оплатить» / «Отменить» запоминаются у заказа: когда его
оплатят или отменят, кнопки снимет фоновая задача.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot import ui
from bot.callbacks import OrderCB
from bot.keyboards.common import BTN_MY_ORDERS, main_menu
from core.clock import format_date, format_datetime
from core.enums import CancelReason, OrderStatus, ReceiptStatus
from core.errors import DomainError
from core.labels import order_status_label
from core.models import Customer, Order, OrderItem, Receipt, Shipment
from core.money import format_rubles
from core.services import notifications, orders, payments

router = Router(name="my_orders")

#: Условный этап «В пункте выдачи»: это не статус заказа, а отметка arrived_at.
ARRIVED_STEP = "arrived"

#: Лента этапов в карточке заказа — то, что видит клиент.
PROGRESS_STEPS: tuple[tuple[OrderStatus | str, str], ...] = (
    (OrderStatus.QUEUED, "Оплачен и принят в работу"),
    (OrderStatus.ASSEMBLING, "Собирается"),
    (OrderStatus.READY, "Готов к отправке"),
    (OrderStatus.SHIPPED, "Передан в доставку"),
    (ARRIVED_STEP, "Доставлен в пункт выдачи"),
    (OrderStatus.COMPLETED, "Получен"),
)

#: Сколько заказов показываем в списке.
ORDERS_LIMIT = 10


@router.message(F.text == BTN_MY_ORDERS)
async def list_orders(message: Message, session: AsyncSession, customer: Customer) -> None:
    result = await session.scalars(
        select(Order)
        .options(
            selectinload(Order.items).selectinload(OrderItem.addons), selectinload(Order.addons)
        )
        .where(Order.customer_id == customer.id, Order.status != OrderStatus.DRAFT)
        .order_by(Order.created_at.desc())
        .limit(ORDERS_LIMIT),
    )
    found = list(result)

    if not found:
        await message.answer(
            "Заказов пока нет. Загляните в каталог — соберём для вас бокс 💗",
            reply_markup=main_menu(),
        )
        return

    builder = InlineKeyboardBuilder()
    lines = ["<b>📦 Ваши заказы</b>", ""]
    for order in found:
        label = _status_label(order)
        lines.append(f"{_title(order)} · {label}")
        builder.button(
            text=f"{_title(order)} — {label}",
            callback_data=OrderCB(order_id=order.id, action="open").pack(),
        )
    builder.adjust(1)

    await message.answer("\n".join(lines), reply_markup=builder.as_markup())


@router.callback_query(OrderCB.filter(F.action == "open"))
async def open_order(
    callback: CallbackQuery,
    callback_data: OrderCB,
    session: AsyncSession,
    customer: Customer,
) -> None:
    order = await _get_own_order(session, callback_data.order_id, customer)
    if order is None:
        await callback.answer("Заказ не найден", show_alert=True)
        return

    await callback.answer()
    shipment = await session.scalar(
        select(Shipment)
        .where(Shipment.order_id == order.id)
        .order_by(Shipment.created_at.desc())
        .limit(1),
    )
    receipt = await session.scalar(
        select(Receipt)
        .where(Receipt.order_id == order.id, Receipt.status == ReceiptStatus.REGISTERED)
        .order_by(Receipt.created_at.desc())
        .limit(1),
    )
    text = _order_card(order, shipment, receipt)
    keyboard = await _order_keyboard(session, order)
    if callback.message is not None:
        sent = await callback.message.answer(
            text,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        if keyboard is not None:
            orders.remember_bot_message(
                order,
                chat_id=sent.chat.id,
                message_id=sent.message_id,
                text=text,
            )


@router.callback_query(OrderCB.filter(F.action == "pay"))
async def pay_order(
    callback: CallbackQuery,
    callback_data: OrderCB,
    session: AsyncSession,
    customer: Customer,
) -> None:
    """Повторная ссылка на оплату для неоплаченного заказа."""
    order = await _get_own_order(session, callback_data.order_id, customer)
    if order is None or order.status != OrderStatus.WAITING_PAYMENT:
        await callback.answer("Этот заказ уже нельзя оплатить", show_alert=True)
        await _strip(callback)
        return

    try:
        payment = await payments.create_payment(session, order)
    except DomainError as error:
        await callback.answer(error.message, show_alert=True)
        return

    await callback.answer()
    url = payments.payment_url(payment)
    text, keyboard = _payment_link(order, url)
    if callback.message is not None:
        sent = await callback.message.answer(text, reply_markup=keyboard)
        orders.remember_bot_message(
            order,
            chat_id=sent.chat.id,
            message_id=sent.message_id,
            text=text,
        )


@router.callback_query(OrderCB.filter(F.action == "cancel"))
async def cancel_order(
    callback: CallbackQuery,
    callback_data: OrderCB,
    session: AsyncSession,
    customer: Customer,
) -> None:
    """Клиент может отменить только то, что ещё не оплачено."""
    order = await _get_own_order(session, callback_data.order_id, customer)
    if order is None:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    if order.status != OrderStatus.WAITING_PAYMENT:
        await callback.answer(
            "Оплаченный заказ отменяем вручную — напишите нам, пожалуйста."
            if order.paid_at
            else "Этот заказ уже отменён.",
            show_alert=True,
        )
        await _strip(callback)
        return

    await orders.cancel(
        session,
        order,
        reason=CancelReason.CUSTOMER,
        actor=orders.ACTOR_BOT,
        comment="отменён клиентом",
    )
    await callback.answer("Заказ отменён")
    await ui.mark_choice(callback.message, "✖️ Заказ отменён")


async def _strip(callback: CallbackQuery) -> None:
    """Снять кнопки с сообщения, по которому нажали: по ним больше нечего делать."""
    if callback.message is not None:
        await ui.strip_buttons(callback.bot, callback.message.chat.id, callback.message.message_id)


def _payment_link(order: Order, url: str) -> tuple[str, InlineKeyboardMarkup | None]:
    """Сообщение со ссылкой на оплату: кнопкой, а если адрес локальный — текстом."""
    text = f"Заказ {order.display_number} на {format_rubles(order.total_kopecks)}."
    if not payments.is_button_url(url):
        return f"{text}\n\nСсылка на оплату:\n<code>{url}</code>", None
    builder = InlineKeyboardBuilder()
    builder.button(text="💳 Оплатить", url=url)
    return text, builder.as_markup()


def _title(order: Order) -> str:
    """«Заказ EM-1025», а до оплаты — «Заказ на 1 700 ₽»."""
    return f"Заказ {order.display_number}"


def _status_label(order: Order) -> str:
    if order.refunded_at is not None:
        return "оплата возвращена"
    if order.status == OrderStatus.SHIPPED and order.arrived_at is not None:
        return "в пункте выдачи"
    return order_status_label(order.status).lower()


def _order_card(order: Order, shipment: Shipment | None, receipt: Receipt | None) -> str:
    lines = [
        f"<b>{_title(order)}</b>",
        f"от {format_datetime(order.created_at)}",
        "",
        notifications.describe_items(order),
        "",
        f"Итого: {format_rubles(order.total_kopecks)}",
    ]

    snapshot = order.pickup_snapshot or {}
    if snapshot:
        lines.append(f"Пункт выдачи: {snapshot.get('address', '')}")

    if order.status == OrderStatus.CANCELLED:
        lines.extend(["", f"Статус: {_status_label(order)}"])
        return "\n".join(lines)
    if order.status == OrderStatus.WAITING_PAYMENT:
        lines.extend(["", "Ждём оплату."])
        if order.expires_at:
            lines.append(f"Ссылка действует до {format_datetime(order.expires_at)}.")
        return "\n".join(lines)

    lines.extend(["", _progress(order)])
    if order.promised_ready_date and order.status in {OrderStatus.QUEUED, OrderStatus.ASSEMBLING}:
        lines.append(f"\nПланируем собрать к {format_date(order.promised_ready_date)}")
    if shipment is not None and shipment.track_number:
        lines.append(f"\nТрек-номер: <code>{shipment.track_number}</code>")
    if receipt is not None and receipt.url:
        lines.append(f'\n🧾 <a href="{receipt.url}">Чек об оплате</a>')
    return "\n".join(lines)


def _progress(order: Order) -> str:
    """Лента этапов: пройденные — галочкой, текущий — стрелкой."""
    current: OrderStatus | str = OrderStatus(order.status)
    if current == OrderStatus.SHIPPED and order.arrived_at is not None:
        current = ARRIVED_STEP
    order_of = {status: index for index, (status, _) in enumerate(PROGRESS_STEPS)}
    position = order_of.get(current, -1)

    parts = []
    for index, (_, label) in enumerate(PROGRESS_STEPS):
        if index < position or (index == position and current == OrderStatus.COMPLETED):
            parts.append(f"✅ {label}")
        elif index == position:
            parts.append(f"🔄 <b>{label}</b>")
        else:
            parts.append(f"▫️ {label}")
    return "\n".join(parts)


async def _order_keyboard(session: AsyncSession, order: Order) -> InlineKeyboardMarkup | None:
    """Кнопки под карточкой. У оплаченного заказа действий нет — только чтение."""
    if order.status != OrderStatus.WAITING_PAYMENT:
        return None

    builder = InlineKeyboardBuilder()
    payment = await payments.get_pending_payment(session, order.id)
    url = payments.payment_url(payment) if payment is not None else ""
    if url and payments.is_button_url(url):
        builder.button(text="💳 Оплатить", url=url)
    else:
        # Ссылки ещё нет или она локальная — кнопка запросит её сообщением.
        builder.button(
            text="💳 Оплатить",
            callback_data=OrderCB(order_id=order.id, action="pay").pack(),
        )
    builder.button(
        text="Отменить заказ",
        callback_data=OrderCB(order_id=order.id, action="cancel").pack(),
    )
    builder.adjust(1)
    return builder.as_markup()


async def _get_own_order(
    session: AsyncSession,
    order_id: int,
    customer: Customer,
) -> Order | None:
    """Заказ клиента. Чужой заказ по угаданному id открыть нельзя."""
    return await session.scalar(
        select(Order)
        .options(*orders.ORDER_LOAD_OPTIONS)
        .where(Order.id == order_id, Order.customer_id == customer.id),
    )
