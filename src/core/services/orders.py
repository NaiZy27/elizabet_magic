"""Заказы: черновик, оформление, смена этапов.

Единственное место, где меняется статус заказа. Каждая смена пишет событие в историю
и ставит уведомление клиенту — уведомление уходит только после успешного коммита.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any

from core.services import board, settings
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.clock import now_utc
from core.enums import (
    ALLOWED_TRANSITIONS,
    QUEUE_STATUSES,
    STATUS_TEMPLATES,
    MessageTemplateKey,
    OrderEventType,
    OrderStatus,
)
from core.errors import ConflictError, NotFoundError, ValidationError
from core.models import (
    ORDER_NUMBER_PREFIX,
    Customer,
    Order,
    OrderAddon,
    OrderEvent,
    OrderItem,
    PickupPoint,
    order_number_seq,
)
from core.services import notifications
from core.services.catalog import RequestedItem, price_order

#: Кто выполнил действие — пишется в историю заказа.
ACTOR_BOT = "bot"
ACTOR_SYSTEM = "system"


def admin_actor(admin_id: int) -> str:
    return f"admin:{admin_id}"


# --- клиент ---


async def get_or_create_customer(
    session: AsyncSession,
    *,
    telegram_user_id: int,
    username: str | None = None,
    full_name: str | None = None,
) -> Customer:
    """Найти клиента по Telegram-id или завести нового.

    Имя и ник обновляем при каждом заходе: в Telegram их меняют.
    """
    customer = await session.scalar(
        select(Customer).where(Customer.telegram_user_id == telegram_user_id),
    )
    if customer is None:
        customer = Customer(
            telegram_user_id=telegram_user_id,
            username=username,
            full_name=full_name,
        )
        session.add(customer)
        await session.flush()
        return customer

    customer.username = username
    if full_name:
        customer.full_name = full_name
    # Раз клиент пишет боту, значит больше не заблокирован.
    customer.bot_blocked_at = None
    return customer


async def accept_consent(session: AsyncSession, customer: Customer) -> None:
    """Отметить согласие на обработку персональных данных."""
    if customer.consent_accepted_at is None:
        customer.consent_accepted_at = now_utc()
        await session.flush()


# --- черновик ---


async def get_order(session: AsyncSession, order_id: int) -> Order:
    order = await session.scalar(
        select(Order)
        .options(
            selectinload(Order.items),
            selectinload(Order.addons),
            selectinload(Order.customer),
        )
        .where(Order.id == order_id),
    )
    if order is None:
        raise NotFoundError("Заказ не найден")
    return order


async def get_order_by_number(session: AsyncSession, number: str) -> Order | None:
    return await session.scalar(select(Order).where(Order.number == number))


async def get_draft(session: AsyncSession, customer_id: int) -> Order | None:
    """Незавершённое оформление клиента. Он у клиента может быть только один."""
    return await session.scalar(
        select(Order)
        .options(selectinload(Order.items), selectinload(Order.addons))
        .where(Order.customer_id == customer_id, Order.status == OrderStatus.DRAFT),
    )


async def create_draft(session: AsyncSession, customer: Customer) -> Order:
    """Начать оформление. Старый черновик заменяется новым."""
    existing = await get_draft(session, customer.id)
    if existing is not None:
        await session.delete(existing)
        await session.flush()

    order = Order(
        customer_id=customer.id,
        status=OrderStatus.DRAFT,
        recipient_name=customer.full_name,
        recipient_phone=customer.phone,
        recipient_email=customer.email,
    )
    session.add(order)
    await session.flush()
    await log_event(session, order, OrderEventType.CREATED, actor=ACTOR_BOT)
    return order


async def set_selection(
    session: AsyncSession,
    order: Order,
    *,
    items: Sequence[RequestedItem],
    addon_ids: Sequence[int] = (),
) -> Order:
    """Записать в черновик выбранные боксы и услуги, пересчитав суммы.

    Цены берутся из каталога, а не из того, что прислал клиент.
    """
    _ensure_draft(order)
    priced = await price_order(
        session,
        items=items,
        addon_ids=addon_ids,
        delivery_kopecks=order.delivery_kopecks,
    )

    order.items.clear()
    order.addons.clear()
    await session.flush()

    for priced_item in priced.items:
        order.items.append(
            OrderItem(
                variant_id=priced_item.variant.id,
                quantity=priced_item.quantity,
                spoon_count=priced_item.spoon_count,
                unit_price_kopecks=priced_item.unit_price_kopecks,
                name_snapshot=priced_item.name_snapshot,
            ),
        )
    for priced_addon in priced.addons:
        order.addons.append(
            OrderAddon(
                addon_id=priced_addon.addon.id,
                quantity=priced_addon.quantity,
                unit_price_kopecks=priced_addon.unit_price_kopecks,
                name_snapshot=priced_addon.name_snapshot,
                code_snapshot=priced_addon.addon.code,
                charge_mode_snapshot=priced_addon.addon.charge_mode,
            ),
        )

    order.subtotal_kopecks = priced.subtotal_kopecks
    order.total_kopecks = order.subtotal_kopecks + order.delivery_kopecks
    order.production_days = priced.production_days
    await session.flush()
    return order


async def set_wishes(
    session: AsyncSession,
    order: Order,
    *,
    favorite_color_ids: Sequence[int] = (),
    favorite_custom: str | None = None,
    avoid_color_ids: Sequence[int] = (),
    avoid_custom: str | None = None,
    comment: str | None = None,
) -> Order:
    """Пожелания клиента: цвета из палитры плюс свободный текст."""
    _ensure_draft(order)
    order.favorite_colors = {"colors": list(favorite_color_ids), "custom": favorite_custom or ""}
    order.avoid_colors = {"colors": list(avoid_color_ids), "custom": avoid_custom or ""}
    order.customer_comment = (comment or "").strip() or None
    await session.flush()
    return order


async def set_recipient(
    session: AsyncSession,
    order: Order,
    *,
    name: str,
    phone: str,
    email: str | None = None,
    customer: Customer | None = None,
) -> Order:
    """Контакты получателя. Телефон обязателен — его требует служба доставки."""
    _ensure_draft(order)
    if not name.strip():
        raise ValidationError("Укажите имя получателя")
    if not phone.strip():
        raise ValidationError("Нужен телефон: без него служба доставки не примет посылку")

    order.recipient_name = name.strip()
    order.recipient_phone = phone.strip()
    order.recipient_email = (email or "").strip() or None

    # Запоминаем в карточке клиента, чтобы в следующий раз предложить эти же данные.
    if customer is not None:
        customer.full_name = order.recipient_name
        customer.phone = order.recipient_phone
        if order.recipient_email:
            customer.email = order.recipient_email

    await session.flush()
    return order


async def set_pickup_point(
    session: AsyncSession,
    order: Order,
    *,
    point: PickupPoint,
    delivery_kopecks: int,
    provider_name: str,
    is_fallback_price: bool = False,
) -> Order:
    """Выбранный пункт выдачи и стоимость доставки.

    Справочник ПВЗ обновляется каждую ночь, поэтому в заказе остаётся снимок:
    адрес и режим работы такими, какими их видел клиент.
    """
    _ensure_draft(order)
    if delivery_kopecks < 0:
        raise ValidationError("Стоимость доставки не может быть отрицательной")

    order.pickup_point_id = point.id
    order.delivery_provider = point.provider
    order.pickup_snapshot = {
        "provider": str(point.provider),
        "provider_name": provider_name,
        "external_id": point.external_id,
        "name": point.name,
        "city": point.city,
        "address": point.address,
        "working_hours": point.working_hours or "",
    }
    order.delivery_kopecks = delivery_kopecks
    order.total_kopecks = order.subtotal_kopecks + delivery_kopecks
    await session.flush()

    if is_fallback_price:
        await log_event(
            session,
            order,
            OrderEventType.DELIVERY_PRICE_FALLBACK,
            payload={"delivery_kopecks": delivery_kopecks},
            actor=ACTOR_SYSTEM,
        )
    return order


# --- оформление ---


async def submit(session: AsyncSession, order: Order, *, actor: str = ACTOR_BOT) -> Order:
    """Перевести черновик в «Ожидает оплаты»: присвоить номер и срок оплаты."""
    _ensure_draft(order)
    _check_ready_to_submit(order)

    ttl = await settings.get_unpaid_ttl(session)
    order.number = await _next_order_number(session)
    order.expires_at = now_utc() + ttl

    # Уведомление «заказ оформлен» бот отправляет сам: к нему нужна кнопка оплаты,
    # а ссылка появляется вместе с платежом уже после оформления.
    await change_status(session, order, OrderStatus.WAITING_PAYMENT, actor=actor, notify=False)
    await log_event(
        session,
        order,
        OrderEventType.SUBMITTED,
        payload={"number": order.number, "total_kopecks": order.total_kopecks},
        actor=actor,
    )
    return order


async def mark_paid(session: AsyncSession, order: Order, *, actor: str = ACTOR_SYSTEM) -> Order:
    """Подтверждённая оплата: заказ встаёт в очередь, клиенту называется дата.

    Обещанная дата фиксируется один раз и дальше не пересчитывается, даже если
    очередь сдвинется: это то, что мы сказали клиенту.
    """
    if order.status == OrderStatus.QUEUED and order.paid_at is not None:
        # Повторное уведомление об оплате — ничего не делаем.
        return order
    if order.status != OrderStatus.WAITING_PAYMENT:
        raise ConflictError(f"Заказ в состоянии «{order.status}» нельзя отметить оплаченным")

    order.paid_at = now_utc()
    order.expires_at = None
    await change_status(session, order, OrderStatus.QUEUED, actor=actor, notify=False)

    calendar = await settings.get_working_calendar(session)
    ready_dates = await board.ready_dates_for_queue(session, calendar=calendar)
    order.promised_ready_date = ready_dates.get(order.id)
    await session.flush()

    await log_event(
        session,
        order,
        OrderEventType.PAYMENT_SUCCEEDED,
        payload={
            "queue_position": order.queue_position,
            "promised_ready_date": (
                order.promised_ready_date.isoformat() if order.promised_ready_date else None
            ),
        },
        actor=actor,
    )
    notifications.schedule_customer_notification(session, order, MessageTemplateKey.ORDER_PAID)
    notifications.schedule_owner_notification(session, order)
    return order


async def change_status(
    session: AsyncSession,
    order: Order,
    new_status: OrderStatus,
    *,
    actor: str = ACTOR_SYSTEM,
    notify: bool = True,
    template_key: MessageTemplateKey | None = None,
    payload: dict[str, Any] | None = None,
) -> Order:
    """Сменить этап заказа. Единственный допустимый способ это сделать."""
    old_status = OrderStatus(order.status)
    if new_status == old_status:
        return order
    if new_status not in ALLOWED_TRANSITIONS[old_status]:
        raise ValidationError(
            f"Нельзя перевести заказ из «{old_status}» в «{new_status}»",
        )

    order.status = new_status
    order.status_changed_at = now_utc()
    if new_status == OrderStatus.READY:
        order.ready_at = now_utc()
    if new_status == OrderStatus.CANCELLED:
        order.cancelled_at = now_utc()

    # Место в очереди появляется вместе со статусом и вместе с ним исчезает:
    # база не разрешит позицию у заказа вне очереди.
    was_queued = old_status in QUEUE_STATUSES
    now_queued = new_status in QUEUE_STATUSES
    if now_queued and not was_queued:
        await board.append_to_queue(session, order)
    elif was_queued and not now_queued:
        await board.remove_from_queue(session, order)
    else:
        await session.flush()

    await log_event(
        session,
        order,
        OrderEventType.STATUS_CHANGED,
        payload={"from": old_status.value, "to": new_status.value, **(payload or {})},
        actor=actor,
    )

    if notify:
        key = template_key or STATUS_TEMPLATES.get(new_status)
        if key is not None:
            notifications.schedule_customer_notification(session, order, key)
    return order


async def cancel(
    session: AsyncSession,
    order: Order,
    *,
    actor: str = ACTOR_SYSTEM,
    reason: str | None = None,
) -> Order:
    """Отменить заказ и закрыть неоплаченную попытку оплаты."""
    # Локальный импорт: payments тоже обращается к заказам.
    from core.services import payments

    await payments.cancel_pending(session, order.id)
    return await change_status(
        session,
        order,
        OrderStatus.CANCELLED,
        actor=actor,
        payload={"reason": reason} if reason else None,
    )


async def expire_unpaid(session: AsyncSession, *, now: dt.datetime | None = None) -> list[Order]:
    """Отменить заказы, которые не оплатили вовремя. Вызывается фоновой задачей."""
    moment = now or now_utc()
    result = await session.scalars(
        select(Order)
        .options(selectinload(Order.items), selectinload(Order.addons))
        .where(
            Order.status == OrderStatus.WAITING_PAYMENT,
            Order.expires_at.is_not(None),
            Order.expires_at < moment,
        ),
    )
    expired = list(result)
    for order in expired:
        await cancel(session, order, actor=ACTOR_SYSTEM, reason="истёк срок оплаты")
        await log_event(session, order, OrderEventType.EXPIRED, actor=ACTOR_SYSTEM)
    return expired


async def delete_stale_drafts(session: AsyncSession, *, older_than_days: int = 30) -> int:
    """Убрать брошенные черновики. Они не попадают ни в метрики, ни на доску."""
    threshold = now_utc() - dt.timedelta(days=older_than_days)
    result = await session.scalars(
        select(Order).where(Order.status == OrderStatus.DRAFT, Order.updated_at < threshold),
    )
    drafts = list(result)
    for draft in drafts:
        await session.delete(draft)
    await session.flush()
    return len(drafts)


# --- история ---


async def log_event(
    session: AsyncSession,
    order: Order,
    event_type: OrderEventType,
    *,
    payload: dict[str, Any] | None = None,
    actor: str = ACTOR_SYSTEM,
) -> OrderEvent:
    """Записать значимое действие в историю заказа."""
    event = OrderEvent(
        order_id=order.id,
        event_type=event_type,
        payload=payload or {},
        actor=actor,
    )
    session.add(event)
    await session.flush()
    return event


async def list_events(session: AsyncSession, order_id: int) -> list[OrderEvent]:
    result = await session.scalars(
        select(OrderEvent)
        .where(OrderEvent.order_id == order_id)
        .order_by(OrderEvent.created_at.desc(), OrderEvent.id.desc()),
    )
    return list(result)


# --- проверки ---


def _ensure_draft(order: Order) -> None:
    if order.status != OrderStatus.DRAFT:
        raise ConflictError("Этот заказ уже оформлен, его нельзя изменить")


def _check_ready_to_submit(order: Order) -> None:
    """Всё ли заполнено, чтобы принять заказ."""
    if not order.items:
        raise ValidationError("Выберите бокс")
    if not order.recipient_name or not order.recipient_phone:
        raise ValidationError("Укажите имя и телефон получателя")
    if order.pickup_point_id is None or not order.pickup_snapshot:
        raise ValidationError("Выберите пункт выдачи")
    if order.total_kopecks != order.subtotal_kopecks + order.delivery_kopecks:
        raise ValidationError("Сумма заказа посчитана неверно, начните оформление заново")
    if order.total_kopecks <= 0:
        raise ValidationError("Сумма заказа должна быть больше нуля")


async def _next_order_number(session: AsyncSession) -> str:
    """Следующий номер из отдельной последовательности: EM-1001, EM-1002…"""
    value = await session.scalar(select(order_number_seq.next_value()))
    return f"{ORDER_NUMBER_PREFIX}{value}"
