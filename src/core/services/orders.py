"""Заказы: черновик, боксы в нём, оформление, смена этапов.

Единственное место, где меняется статус заказа. Каждая смена пишет событие в историю
и ставит уведомление клиенту — уведомление уходит только после успешного коммита.

Пока заказ — черновик, после любой правки (бокс добавили, поменяли, убрали, выбрали
другой пункт выдачи) он пересчитывается целиком: цены из каталога, срок и доставка.
Так в итоге никогда не остаётся цена доставки от прошлого состава.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.clock import now_utc
from core.config import get_settings
from core.enums import (
    ALLOWED_TRANSITIONS,
    QUEUE_STATUSES,
    STATUS_TEMPLATES,
    CancelReason,
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
from core.services import board, catalog, delivery, intake, notifications, settings
from core.services.catalog import PricedAddon, RequestedItem

#: Кто выполнил действие — пишется в историю заказа.
ACTOR_BOT = "bot"
ACTOR_SYSTEM = "system"


def admin_actor(admin_id: int) -> str:
    return f"admin:{admin_id}"


#: Как грузить заказ, чтобы пересчёт и показ не ходили в базу лениво.
ORDER_LOAD_OPTIONS = (
    selectinload(Order.items).selectinload(OrderItem.variant),
    selectinload(Order.items).selectinload(OrderItem.addons),
    selectinload(Order.addons),
    selectinload(Order.customer),
)


# --- клиент ---


async def get_or_create_customer(
    session: AsyncSession,
    *,
    telegram_user_id: int,
    username: str | None = None,
    full_name: str | None = None,
) -> Customer:
    """Найти клиента по Telegram-id или завести нового.

    Ник обновляем при каждом заходе: в Telegram его меняют. Имя — только пока
    клиент не указал имя получателя сам: его выбор важнее имени в профиле.
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
    if full_name and not customer.phone:
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
        select(Order).options(*ORDER_LOAD_OPTIONS).where(Order.id == order_id),
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
        .options(*ORDER_LOAD_OPTIONS)
        .where(Order.customer_id == customer_id, Order.status == OrderStatus.DRAFT),
    )


async def create_draft(session: AsyncSession, customer: Customer) -> Order:
    """Начать оформление. Старый черновик заменяется новым.

    Получатель и пункт выдачи сразу подставляются из прошлого заказа: постоянному
    клиенту остаётся только подтвердить их.
    """
    existing = await get_draft(session, customer.id)
    if existing is not None:
        await session.delete(existing)
        await session.flush()

    order = Order(
        customer_id=customer.id,
        status=OrderStatus.DRAFT,
        recipient_name=customer.full_name if customer.phone else None,
        recipient_phone=customer.phone,
        recipient_email=customer.email,
        bot_messages=[],
    )
    order.items = []
    order.addons = []
    session.add(order)
    await session.flush()

    point = await _last_pickup_point(session, customer)
    if point is not None:
        await _apply_pickup_point(session, order, point)

    await log_event(session, order, OrderEventType.CREATED, actor=ACTOR_BOT)
    return order


@dataclass(frozen=True, slots=True)
class BoxRequest:
    """Бокс, как его собрал клиент: комплектация, услуги и пожелания."""

    variant_id: int
    spoon_count: int
    addon_ids: tuple[int, ...] = ()
    favorite_color_ids: tuple[int, ...] = ()
    avoid_color_ids: tuple[int, ...] = ()
    comment: str | None = None


async def save_box(
    session: AsyncSession,
    order: Order,
    box: BoxRequest,
    *,
    item_id: int | None = None,
) -> OrderItem:
    """Добавить бокс в черновик или заменить уже добавленный (item_id)."""
    _ensure_draft(order)

    if item_id is None:
        rules = await settings.get_order_rules(session)
        if order.box_count >= rules.max_boxes_per_order:
            raise ValidationError(
                f"В одном заказе может быть не больше {rules.max_boxes_per_order} боксов",
            )
        item = OrderItem(quantity=1, unit_price_kopecks=0, name_snapshot="", spoon_count=1)
        item.addons = []
        order.items.append(item)
    else:
        item = _find_item(order, item_id)

    # Выбор записываем в позицию, а цены и названия проставит пересчёт.
    item.variant_id = box.variant_id
    item.spoon_count = box.spoon_count
    item.favorite_colors = await catalog.color_names(session, box.favorite_color_ids)
    item.avoid_colors = await catalog.color_names(session, box.avoid_color_ids)
    item.comment = (box.comment or "").strip() or None

    await _recalculate(session, order, item_addon_ids={id(item): box.addon_ids})
    return item


async def remove_box(session: AsyncSession, order: Order, item_id: int) -> None:
    """Убрать бокс из черновика."""
    _ensure_draft(order)
    item = _find_item(order, item_id)
    for addon in list(item.addons):
        _drop_addon(order, addon, item)
    order.items.remove(item)
    await session.flush()
    await _recalculate(session, order)


async def set_recipient(
    session: AsyncSession,
    order: Order,
    *,
    name: str,
    phone: str,
    email: str,
    customer: Customer | None = None,
) -> Order:
    """Контакты получателя. Телефон нужен службе доставки, email — для чека."""
    _ensure_draft(order)
    if not name.strip():
        raise ValidationError("Укажите имя получателя")
    if not phone.strip():
        raise ValidationError("Нужен телефон: без него служба доставки не примет посылку")
    if not is_valid_email(email):
        raise ValidationError("Нужен email: на него придёт чек об оплате")

    order.recipient_name = name.strip()
    order.recipient_phone = phone.strip()
    order.recipient_email = email.strip()

    # Запоминаем в карточке клиента, чтобы в следующий раз подставить эти же данные.
    if customer is not None:
        customer.full_name = order.recipient_name
        customer.phone = order.recipient_phone
        customer.email = order.recipient_email

    await session.flush()
    return order


async def set_pickup_point(session: AsyncSession, order: Order, point: PickupPoint) -> Order:
    """Выбранный пункт выдачи. Стоимость доставки считается тут же."""
    _ensure_draft(order)
    await _apply_pickup_point(session, order, point)
    return order


def is_valid_email(value: str | None) -> bool:
    """Проверка «на опечатку», а не по RFC: одна @, точка в домене, без пробелов."""
    email = (value or "").strip()
    if not email or " " in email or email.count("@") != 1:
        return False
    local, domain = email.split("@")
    return bool(local) and "." in domain.strip(".") and not domain.startswith(".")


# --- пересчёт ---


async def recalculate(session: AsyncSession, order: Order) -> Order:
    """Пересчитать черновик по текущему каталогу и тарифам доставки."""
    _ensure_draft(order)
    await _recalculate(session, order)
    return order


async def _recalculate(
    session: AsyncSession,
    order: Order,
    *,
    item_addon_ids: dict[int, Sequence[int]] | None = None,
) -> None:
    """Цены, услуги, срок и доставка — заново, по всему заказу.

    `item_addon_ids` — новый выбор услуг для конкретных позиций (ключ — id() объекта,
    потому что у только что добавленного бокса ещё нет id в базе). Для остальных
    берём то, что уже записано в заказе.
    """
    overrides = item_addon_ids or {}
    items = list(order.items)

    if not items:
        for addon in list(order.addons):
            _drop_addon(order, addon)
        order.subtotal_kopecks = 0
        order.delivery_kopecks = 0
        order.total_kopecks = 0
        order.production_days = 1
        await session.flush()
        return

    requested = [
        RequestedItem(
            variant_id=item.variant_id,
            quantity=item.quantity,
            spoon_count=item.spoon_count,
            addon_ids=tuple(
                overrides[id(item)]
                if id(item) in overrides
                else (addon.addon_id for addon in item.addons)
            ),
        )
        for item in items
    ]
    priced = await catalog.price_order(
        session,
        items=requested,
        order_addon_ids=[addon.addon_id for addon in order.order_addons],
    )

    for item, priced_item in zip(items, priced.items, strict=True):
        item.variant_id = priced_item.variant.id
        item.variant = priced_item.variant
        item.spoon_count = priced_item.spoon_count
        item.unit_price_kopecks = priced_item.unit_price_kopecks
        item.name_snapshot = priced_item.name_snapshot
        _sync_addons(order, item, item.addons, priced_item.addons)
    _sync_addons(order, None, order.order_addons, priced.addons)

    order.subtotal_kopecks = priced.subtotal_kopecks
    order.production_days = priced.production_days
    # Итог — вместе с частями: база проверяет total = subtotal + delivery на каждой записи.
    order.total_kopecks = order.subtotal_kopecks + order.delivery_kopecks
    await session.flush()
    await _requote_delivery(session, order)


def _sync_addons(
    order: Order,
    item: OrderItem | None,
    current: Sequence[OrderAddon],
    wanted: Sequence[PricedAddon],
) -> None:
    """Привести услуги бокса (или заказа) к посчитанному набору."""
    wanted_by_id = {priced.addon.id: priced for priced in wanted}
    for addon in list(current):
        if addon.addon_id not in wanted_by_id:
            _drop_addon(order, addon, item)

    existing = {addon.addon_id: addon for addon in current if addon.addon_id in wanted_by_id}
    for addon_id, priced in wanted_by_id.items():
        row = existing.get(addon_id)
        if row is None:
            row = OrderAddon(addon_id=addon_id)
            order.addons.append(row)
            if item is not None:
                item.addons.append(row)
        row.quantity = priced.quantity
        row.unit_price_kopecks = priced.unit_price_kopecks
        row.name_snapshot = priced.name_snapshot
        row.code_snapshot = priced.addon.code
        row.charge_mode_snapshot = priced.addon.charge_mode


def _drop_addon(order: Order, addon: OrderAddon, item: OrderItem | None = None) -> None:
    """Убрать услугу из заказа (и из бокса, если она к нему привязана)."""
    if item is not None and addon in item.addons:
        item.addons.remove(addon)
    if addon in order.addons:
        order.addons.remove(addon)


async def _requote_delivery(
    session: AsyncSession,
    order: Order,
) -> delivery.DeliveryQuote | None:
    """Доставка зависит от веса посылки, поэтому считается после состава."""
    quote: delivery.DeliveryQuote | None = None
    if order.pickup_point_id is None or not order.items:
        order.delivery_kopecks = 0
    else:
        point = await session.get(PickupPoint, order.pickup_point_id)
        if point is None or not point.is_active:
            # Пункт пропал из справочника — пусть клиент выберет другой.
            order.pickup_point_id = None
            order.pickup_snapshot = None
            order.delivery_provider = None
            order.delivery_kopecks = 0
        else:
            quote = await delivery.quote(
                session,
                point=point,
                parcel=delivery.parcel_for_order(order),
            )
            order.delivery_kopecks = quote.price_kopecks
    order.total_kopecks = order.subtotal_kopecks + order.delivery_kopecks
    await session.flush()
    return quote


async def _apply_pickup_point(session: AsyncSession, order: Order, point: PickupPoint) -> None:
    """Записать пункт выдачи снимком и пересчитать доставку.

    Справочник ПВЗ обновляется каждую ночь, поэтому в заказе остаётся снимок:
    адрес и режим работы такими, какими их видел клиент.
    """
    provider = await delivery.get_provider(session, point.provider)
    order.pickup_point_id = point.id
    order.delivery_provider = point.provider
    order.pickup_snapshot = {
        "provider": str(point.provider),
        "provider_name": provider.name,
        "external_id": point.external_id,
        "name": point.name,
        "city": point.city,
        "address": point.address,
        "working_hours": point.working_hours or "",
    }
    quote = await _requote_delivery(session, order)
    if quote is not None and quote.is_fallback:
        await log_event(
            session,
            order,
            OrderEventType.DELIVERY_PRICE_FALLBACK,
            payload={"delivery_kopecks": quote.price_kopecks},
            actor=ACTOR_SYSTEM,
        )


async def _last_pickup_point(session: AsyncSession, customer: Customer) -> PickupPoint | None:
    """Пункт из прошлого заказа, если он ещё работает и служба включена."""
    if customer.last_pickup_point_id is None:
        return None
    point = await session.get(PickupPoint, customer.last_pickup_point_id)
    if point is None or not point.is_active:
        return None
    if point.environment != get_settings().delivery_environment:
        return None
    enabled = {provider.code for provider in await delivery.enabled_providers(session)}
    return point if point.provider in enabled else None


def _find_item(order: Order, item_id: int) -> OrderItem:
    item = next((item for item in order.items if item.id == item_id), None)
    if item is None:
        raise NotFoundError("Этого бокса уже нет в заказе")
    return item


# --- оформление ---


async def submit(session: AsyncSession, order: Order, *, actor: str = ACTOR_BOT) -> Order:
    """Перевести черновик в «Ожидает оплаты».

    Перед этим заказ пересчитывается: цены в каталоге могли поменяться, пока клиент
    думал. Номер не присваивается — он появится вместе с оплатой.
    """
    _ensure_draft(order)
    await intake.ensure_open(session)
    await _recalculate(session, order)
    _check_ready_to_submit(order)

    ttl = await settings.get_unpaid_ttl(session)
    order.expires_at = now_utc() + ttl

    # Уведомление «заказ оформлен» бот отправляет сам: к нему нужна кнопка оплаты,
    # а ссылка появляется вместе с платежом уже после оформления.
    await change_status(session, order, OrderStatus.WAITING_PAYMENT, actor=actor, notify=False)
    await log_event(
        session,
        order,
        OrderEventType.SUBMITTED,
        payload={"total_kopecks": order.total_kopecks, "box_count": order.box_count},
        actor=actor,
    )
    return order


async def mark_paid(session: AsyncSession, order: Order, *, actor: str = ACTOR_SYSTEM) -> Order:
    """Подтверждённая оплата: номер, место в очереди и дата для клиента.

    Обещанная дата фиксируется один раз и дальше не пересчитывается, даже если
    очередь сдвинется: это то, что мы сказали клиенту.
    """
    if order.paid_at is not None:
        # Повторное уведомление об оплате — ничего не делаем.
        return order
    if order.status != OrderStatus.WAITING_PAYMENT:
        raise ConflictError(f"Заказ в состоянии «{order.status}» нельзя отметить оплаченным")

    order.paid_at = now_utc()
    order.expires_at = None
    order.number = await _next_order_number(session)
    await change_status(session, order, OrderStatus.QUEUED, actor=actor, notify=False)

    calendar = await settings.get_working_calendar(session)
    ready_dates = await board.ready_dates_for_queue(session, calendar=calendar)
    order.promised_ready_date = intake.promised_date(
        ready_dates.get(order.id),
        intake=await settings.get_intake(session),
        calendar=calendar,
    )

    # В следующий раз этот пункт выдачи подставится сам.
    customer = await session.get(Customer, order.customer_id)
    if customer is not None and order.pickup_point_id is not None:
        customer.last_pickup_point_id = order.pickup_point_id
    await session.flush()

    await log_event(
        session,
        order,
        OrderEventType.PAYMENT_SUCCEEDED,
        payload={
            "number": order.number,
            "queue_position": order.queue_position,
            "promised_ready_date": (
                order.promised_ready_date.isoformat() if order.promised_ready_date else None
            ),
        },
        actor=actor,
    )
    notifications.schedule_strip_buttons(session, order)
    notifications.schedule_customer_notification(session, order, MessageTemplateKey.ORDER_PAID)
    notifications.schedule_owner_notification(session, order)
    return order


def can_revive(order: Order) -> bool:
    """Можно ли вернуть в работу заказ, оплата по которому пришла после отмены.

    Только если его отменили мы сами по сроку оплаты: клиент мог открыть страницу
    оплаты заранее и заплатить позже. Отменённое клиентом или владелицей не трогаем.
    """
    return (
        order.status == OrderStatus.CANCELLED
        and order.cancel_reason == CancelReason.EXPIRED
        and order.paid_at is None
    )


async def revive_expired(
    session: AsyncSession, order: Order, *, actor: str = ACTOR_SYSTEM
) -> Order:
    """Вернуть заказ, отменённый по сроку, в «Ожидает оплаты» — чтобы провести оплату.

    Обходит обычную таблицу переходов: из «Отменён» по правилам дороги нет, но деньги
    клиента уже у нас. Лимит заказов здесь не проверяется по той же причине.
    """
    if not can_revive(order):
        raise ConflictError("Этот заказ нельзя вернуть в работу автоматически")

    order.status = OrderStatus.WAITING_PAYMENT
    order.status_changed_at = now_utc()
    order.cancelled_at = None
    order.cancel_reason = None
    await session.flush()
    await log_event(session, order, OrderEventType.REVIVED, actor=actor)
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
    if new_status == OrderStatus.ARRIVED and order.arrived_at is None:
        order.arrived_at = now_utc()
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
    reason: CancelReason,
    actor: str = ACTOR_SYSTEM,
    comment: str | None = None,
    template_key: MessageTemplateKey | None = None,
) -> Order:
    """Отменить заказ и закрыть неоплаченную попытку оплаты."""
    # Локальный импорт: payments тоже обращается к заказам.
    from core.services import payments

    await payments.cancel_pending(session, order.id)
    order.cancel_reason = reason
    await change_status(
        session,
        order,
        OrderStatus.CANCELLED,
        actor=actor,
        template_key=template_key,
        payload={"reason": comment or reason.value},
    )
    notifications.schedule_strip_buttons(session, order)
    return order


async def mark_arrived(session: AsyncSession, order: Order, *, actor: str = ACTOR_SYSTEM) -> Order:
    """Посылка прибыла в пункт выдачи.

    Это этап заказа: клиенту уходит «ваш бокс в пункте выдачи», карточка переезжает
    в колонку «Доставлен в ПВЗ». Пока служба доставки не подключена, этап ставится
    руками; потом его будет проставлять синхронизация отправлений.
    """
    if order.status not in {OrderStatus.SHIPPED, OrderStatus.ARRIVED}:
        raise ConflictError("Отметить прибытие можно только у заказа, переданного в доставку")
    await log_event(session, order, OrderEventType.ARRIVED_AT_PICKUP, actor=actor)
    return await change_status(session, order, OrderStatus.ARRIVED, actor=actor)


async def expire_unpaid(session: AsyncSession, *, now: dt.datetime | None = None) -> list[Order]:
    """Отменить заказы, которые не оплатили вовремя. Вызывается фоновой задачей."""
    moment = now or now_utc()
    result = await session.scalars(
        select(Order)
        .options(*ORDER_LOAD_OPTIONS)
        .where(
            Order.status == OrderStatus.WAITING_PAYMENT,
            Order.expires_at.is_not(None),
            Order.expires_at < moment,
        ),
    )
    expired = list(result)
    for order in expired:
        await cancel(
            session,
            order,
            reason=CancelReason.EXPIRED,
            actor=ACTOR_SYSTEM,
            comment="истёк срок оплаты",
        )
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


# --- сообщения бота ---


def remember_bot_message(
    order: Order,
    *,
    chat_id: int,
    message_id: int,
    text: str | None = None,
) -> None:
    """Запомнить сообщение с кнопками по заказу, чтобы потом их снять.

    Если передан текст, при снятии кнопок под ним допишется итог: «✅ Оплачено».
    """
    known = {(entry["chat_id"], entry["message_id"]) for entry in order.bot_messages}
    if (chat_id, message_id) in known:
        return
    entry: dict[str, int | str] = {"chat_id": chat_id, "message_id": message_id}
    if text:
        entry["text"] = text
    # Новый список, а не append: иначе SQLAlchemy не заметит изменения JSONB.
    order.bot_messages = [*order.bot_messages, entry]


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
        raise ValidationError("Добавьте хотя бы один бокс")
    if order.pickup_point_id is None or not order.pickup_snapshot:
        raise ValidationError("Выберите пункт выдачи")
    if not order.recipient_name or not order.recipient_phone:
        raise ValidationError("Укажите имя и телефон получателя")
    if not is_valid_email(order.recipient_email):
        raise ValidationError("Укажите email — на него придёт чек")
    if order.total_kopecks != order.subtotal_kopecks + order.delivery_kopecks:
        raise ValidationError("Сумма заказа посчитана неверно, начните оформление заново")
    if order.total_kopecks <= 0:
        raise ValidationError("Сумма заказа должна быть больше нуля")


async def _next_order_number(session: AsyncSession) -> str:
    """Следующий номер из отдельной последовательности: EM-1001, EM-1002…"""
    value = await session.scalar(select(order_number_seq.next_value()))
    return f"{ORDER_NUMBER_PREFIX}{value}"
