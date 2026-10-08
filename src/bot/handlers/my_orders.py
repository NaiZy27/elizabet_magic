"""«Мои заказы»: список, карточка с лентой этапов, чек, оплата и отмена.

Сообщения с кнопками «Оплатить» / «Отменить» запоминаются у заказа: когда его
оплатят или отменят, кнопки снимет фоновая задача.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot import rich, ui
from bot.callbacks import MenuCB, OrderCB, ReviewCB
from bot.keyboards.common import BTN_MY_ORDERS, DANGER, PRIMARY, SUCCESS, menu_button
from core.clock import format_date, format_datetime
from core.enums import CancelReason, OrderStatus, ReceiptStatus
from core.errors import DomainError
from core.labels import order_status_label
from core.models import Customer, Order, OrderItem, Receipt, Shipment
from core.money import format_rubles
from core.services import notifications, orders, payments

router = Router(name="my_orders")

#: Лента этапов в карточке заказа — то, что видит клиент.
PROGRESS_STEPS: tuple[tuple[OrderStatus, str], ...] = (
    (OrderStatus.QUEUED, "Оплачен и принят в работу"),
    (OrderStatus.ASSEMBLING, "Собирается"),
    (OrderStatus.READY, "Готов к отправке"),
    (OrderStatus.SHIPPED, "Передан в доставку"),
    (OrderStatus.ARRIVED, "Доставлен в пункт выдачи"),
    (OrderStatus.COMPLETED, "Получен"),
)

#: Сколько заказов показываем в списке.
ORDERS_LIMIT = 10


@router.callback_query(MenuCB.filter(F.section == "orders"))
async def orders_button(callback: CallbackQuery, session: AsyncSession, customer: Customer) -> None:
    await callback.answer()
    await list_orders(callback, session, customer)


@router.message(F.text == BTN_MY_ORDERS)
async def list_orders(
    event: Message | CallbackQuery,
    session: AsyncSession,
    customer: Customer,
) -> None:
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
        builder = InlineKeyboardBuilder()
        builder.row(menu_button("🛍 Заказать бокс", "order", style=PRIMARY))
        builder.row(menu_button("← Меню", "home", style="link"))
        await ui.reply(event, text="Заказов пока нет 💗", keyboard=builder.as_markup())
        return

    builder = InlineKeyboardBuilder()
    for order in found:
        builder.button(
            text=f"{_title(order)} — {_status_label(order)}",
            callback_data=OrderCB(order_id=order.id, action="open").pack(),
        )
    builder.adjust(1)
    builder.row(menu_button("← Меню", "home", style="link"))
    await ui.reply(event, text="<b>📦 Ваши заказы</b>", keyboard=builder.as_markup())


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
    await ui.reply(callback, text=text, keyboard=keyboard)
    if order.status == OrderStatus.WAITING_PAYMENT and callback.message is not None:
        # Кнопки оплаты снимутся сами, когда заказ оплатят или отменят.
        orders.remember_bot_message(
            order,
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            text=text,
            rich_body=rich.text_html(text),
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
    sent = await ui.reply(callback, text=text, keyboard=keyboard)
    if sent is not None:
        orders.remember_bot_message(
            order,
            chat_id=sent.chat.id,
            message_id=sent.message_id,
            text=text,
            rich_body=rich.text_html(text),
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

    try:
        await orders.cancel(
            session,
            order,
            reason=CancelReason.CUSTOMER,
            actor=orders.ACTOR_BOT,
            comment="отменён клиентом",
            only_unpaid=True,
        )
    except DomainError:
        # Оплата успела пройти, пока клиент нажимал «Отменить».
        await callback.answer(
            "Оплата уже прошла — заказ в работе. Если передумали, напишите нам.",
            show_alert=True,
        )
        await _strip(callback)
        return
    await callback.answer("Заказ отменён")
    await ui.mark_choice(callback.message, "✖️ Заказ отменён")


async def _strip(callback: CallbackQuery) -> None:
    """Снять кнопки с сообщения, по которому нажали: по ним больше нечего делать."""
    if callback.message is not None:
        await ui.strip_buttons(
            callback.bot,
            callback.message.chat.id,
            callback.message.message_id,
            body=ui.message_body(callback.message),
        )


def _payment_link(order: Order, url: str) -> tuple[str, InlineKeyboardMarkup | None]:
    """Сообщение со ссылкой на оплату: кнопкой, а если адрес локальный — текстом."""
    text = f"Заказ {order.display_number} на {format_rubles(order.total_kopecks)}."
    if not payments.is_button_url(url):
        return f"{text}\n\nСсылка на оплату:\n<code>{url}</code>", None
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="💳 Оплатить", url=url, style=SUCCESS))
    return text, builder.as_markup()


def _title(order: Order) -> str:
    """«Заказ EM-1025», а до оплаты — «Заказ на 1 700 ₽»."""
    return f"Заказ {order.display_number}"


def _status_label(order: Order) -> str:
    if order.refunded_at is not None:
        return "оплата возвращена"
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
        lines.append(f"\nСоберём к {format_date(order.promised_ready_date)}")
    if order.queue_position and order.status == OrderStatus.QUEUED:
        lines.append(f"Место в очереди: {order.queue_position}")
    if shipment is not None and shipment.track_number:
        lines.append(f"\nТрек-номер: <code>{shipment.track_number}</code>")
    if order.status == OrderStatus.ARRIVED and (order.pickup_snapshot or {}).get("working_hours"):
        lines.append(f"Режим работы пункта: {order.pickup_snapshot['working_hours']}")
    if receipt is not None and receipt.url:
        lines.append(f'\n🧾 <a href="{receipt.url}">Чек об оплате</a>')
    return "\n".join(lines)


def _progress(order: Order) -> str:
    """Лента этапов: пройденные — галочкой, текущий — стрелкой."""
    current = OrderStatus(order.status)
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


async def _order_keyboard(session: AsyncSession, order: Order) -> InlineKeyboardMarkup:
    """Кнопки под карточкой: оплатить неоплаченный, оставить отзыв о полученном."""
    builder = InlineKeyboardBuilder()
    if order.status == OrderStatus.COMPLETED:
        builder.button(
            text="💌 Оставить отзыв",
            callback_data=ReviewCB(action="new", order_id=order.id).pack(),
        )
    if order.status != OrderStatus.WAITING_PAYMENT:
        builder.button(
            text="← Мои заказы", callback_data=MenuCB(section="orders").pack(), style="link"
        )
        builder.adjust(1)
        return builder.as_markup()

    payment = await payments.get_pending_payment(session, order.id)
    url = payments.payment_url(payment) if payment is not None else ""
    if url and payments.is_button_url(url):
        builder.row(InlineKeyboardButton(text="💳 Оплатить", url=url, style=SUCCESS))
    else:
        # Ссылки ещё нет или она локальная — кнопка запросит её сообщением.
        builder.row(
            InlineKeyboardButton(
                text="💳 Оплатить",
                callback_data=OrderCB(order_id=order.id, action="pay").pack(),
                style=SUCCESS,
            ),
        )
    builder.row(
        InlineKeyboardButton(
            text="Отменить заказ",
            callback_data=OrderCB(order_id=order.id, action="cancel").pack(),
            style=DANGER,
        ),
    )
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
