"""Отправка заказа: вручную или через API службы.

Когда заказ переходит в «Готов»:
- служба по API (СДЭК с ключами) — фоновая задача создаёт отправление; номер СДЭК
  приходит владелице в бот, она сдаёт посылку в свой пункт по этому номеру;
- ручная служба (Яндекс, Wildberries, Ozon до ключей, СДЭК без ключей) — владелице
  в бот уходят готовые данные: служба, пункт с кодом, получатель и телефон. Она сама
  оформляет отправку в приложении службы.

Статусы отправления СДЭК приходят вебхуком и сверяются фоновой задачей. По ним
заказ сам переходит по этапам: «Передан в доставку» → «Доставлен в ПВЗ» → «Получен»,
и клиенту уходят те же уведомления, что и при ручной смене этапа.
"""

from __future__ import annotations

import logging
from html import escape
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.clock import now_utc
from core.config import get_settings
from core.db import after_commit
from core.enums import (
    ACTIVE_SHIPMENT_STATUSES,
    DeliveryProviderCode,
    MessageTemplateKey,
    OrderEventType,
    OrderStatus,
    ShipmentStatus,
)
from core.errors import ConflictError, DomainError, ExternalServiceError
from core.models import Order, Shipment
from core.services import cdek, delivery, notifications
from core.services import orders as orders_service
from core.text import phone_e164, phone_pretty

logger = logging.getLogger(__name__)

#: Этапы, по которым заказ двигается вслед за посылкой.
_DELIVERY_PATH = (
    OrderStatus.READY,
    OrderStatus.SHIPPED,
    OrderStatus.ARRIVED,
    OrderStatus.COMPLETED,
)


# --- данные для отправки ---


def _point(order: Order) -> dict[str, Any]:
    return order.pickup_snapshot or {}


def recipient_line(order: Order) -> str:
    """«Анна Иванова · +7 900 123-45-67»."""
    parts = [escape(order.recipient_name or "—")]
    if order.recipient_phone:
        parts.append(f"<code>{escape(phone_pretty(order.recipient_phone))}</code>")
    return " · ".join(parts)


def point_line(order: Order) -> str:
    """«г. Москва, Нагорная ул., 15 · код 2500» — адрес и код копируются тапом."""
    point = _point(order)
    address = ", ".join(part for part in (point.get("city"), point.get("address")) if part)
    line = f"<code>{escape(address or '—')}</code>"
    if point.get("external_id"):
        line += f" · код <code>{escape(str(point['external_id']))}</code>"
    return line


def owner_delivery_lines(order: Order) -> list[str]:
    """Блок «Доставка» в карточке нового заказа для владелицы."""
    point = _point(order)
    provider = escape(str(point.get("provider_name") or "—"))
    if delivery.is_automatic(order.delivery_provider):
        how = "отправление создастся само, когда заказ будет «Готов»"
    else:
        how = "отправить вручную"
    return [
        f"🚚 {provider} · {how}",
        f"📍 {point_line(order)}",
        f"👤 {recipient_line(order)}",
    ]


def manual_dispatch_text(order: Order, *, reason: str | None = None) -> str:
    """Всё, что нужно владелице, чтобы оформить отправку в приложении службы."""
    point = _point(order)
    provider = escape(str(point.get("provider_name") or "службу доставки"))
    label = escape(order.admin_label)
    lines = [f"📦 <b>Заказ {label} готов — отправьте вручную через {provider}</b>"]
    if reason:
        lines.append(reason)
    lines += ["", f"📍 Пункт: {point_line(order)}"]
    if point.get("working_hours"):
        lines.append(f"🕘 {escape(str(point['working_hours']))}")
    lines += [
        f"👤 Получатель: {recipient_line(order)}",
        "",
        escape(notifications.describe_items(order)),
    ]
    return "\n".join(lines)


# --- «Готов»: создать отправление или прислать данные ---


def on_ready(session: AsyncSession, order: Order) -> None:
    """Заказ собран. Вызывается из смены этапа — действия выполнятся после коммита."""
    if order.delivery_provider is None:
        return
    if delivery.is_automatic(order.delivery_provider):
        schedule_create(session, order)
    else:
        notifications.schedule_owner_text(session, manual_dispatch_text(order))


def schedule_create(session: AsyncSession, order: Order) -> None:
    """Поставить создание отправления СДЭК в очередь — после коммита."""
    order_id = order.id

    async def _enqueue() -> None:
        from worker.tasks.shipments import create_shipment

        await create_shipment.kiq(order_id=order_id)

    after_commit(session, _enqueue)


async def active_shipment(session: AsyncSession, order_id: int) -> Shipment | None:
    return await session.scalar(
        select(Shipment).where(
            Shipment.order_id == order_id,
            Shipment.status != ShipmentStatus.CANCELLED,
        ),
    )


def _cdek_payload(order: Order, number: str) -> dict[str, Any]:
    """Заказ для СДЭК: тип «интернет-магазин», тариф склад-склад, одна посылка."""
    settings = get_settings().cdek
    point = _point(order)
    parcel = delivery.parcel_for_order(order)
    phone = phone_e164(order.recipient_phone)
    if phone is None:
        raise ConflictError("У получателя неверный телефон — СДЭК без него заказ не примет")
    if not point.get("external_id"):
        raise ConflictError("В заказе нет кода пункта выдачи")

    units = max(1, sum(item.quantity for item in order.items))
    unit_weight = max(1, parcel.weight_g // units)
    items = [
        {
            "name": item.name_snapshot[:255],
            "ware_key": (item.variant.sku if item.variant else f"item-{item.id}")[:50],
            # Заказ оплачен заранее — при получении брать нечего.
            "payment": {"value": 0},
            # Объявленная стоимость: за неё СДЭК страхует посылку.
            "cost": round(item.total_kopecks / item.quantity / 100, 2),
            "weight": unit_weight,
            "amount": item.quantity,
        }
        for item in order.items
    ]
    recipient: dict[str, Any] = {
        "name": (order.recipient_name or "Получатель")[:255],
        "phones": [{"number": phone}],
    }
    if order.recipient_email:
        recipient["email"] = order.recipient_email
    return {
        "type": cdek.ORDER_TYPE_SHOP,
        "number": number,
        "tariff_code": settings.tariff_code,
        "shipment_point": settings.shipment_point,
        "delivery_point": str(point["external_id"]),
        "recipient": recipient,
        "packages": [
            {
                "number": "1",
                "weight": parcel.weight_g,
                "length": parcel.length_cm,
                "width": parcel.width_cm,
                "height": parcel.height_cm,
                "items": items,
            },
        ],
    }


async def create_cdek_shipment(session: AsyncSession, order: Order) -> Shipment:
    """Создать отправление СДЭК. Повторный вызов вернёт уже созданное.

    Сначала блокируем заказ: кнопка в панели и автоматический запуск не создадут
    два отправления одновременно.
    """
    await orders_service.lock(session, order)
    existing = await active_shipment(session, order.id)
    if existing is not None:
        return existing
    if order.delivery_provider != DeliveryProviderCode.CDEK:
        raise ConflictError("Заказ едет не через СДЭК")
    if not delivery.is_automatic(DeliveryProviderCode.CDEK):
        raise ConflictError("СДЭК не подключён по API: нет ключей или пункта сдачи")
    if order.status not in {OrderStatus.READY, OrderStatus.SHIPPED}:
        raise ConflictError("Отправление создаётся для собранного заказа")

    # Номер заказа у СДЭК: повторная попытка после отклонённой — с суффиксом «-2», «-3».
    tried = await session.scalar(
        select(func.count(Shipment.id)).where(Shipment.order_id == order.id),
    )
    number = order.admin_label if not tried else f"{order.admin_label}-{tried + 1}"

    uuid = await cdek.create_order(_cdek_payload(order, number))
    shipment = Shipment(
        order_id=order.id,
        provider=DeliveryProviderCode.CDEK,
        external_id=uuid,
        status=ShipmentStatus.CREATED,
        status_raw="Принят СДЭК, ждём номер",
    )
    session.add(shipment)
    await session.flush()
    await orders_service.log_event(
        session,
        order,
        OrderEventType.SHIPMENT_CREATED,
        payload={"provider": "cdek", "uuid": uuid, "number": number},
    )
    return shipment


async def report_create_failure(session: AsyncSession, order: Order, error: str) -> None:
    """СДЭК не принял отправление — владелица получает причину и данные для ручной отправки."""
    notifications.schedule_owner_text(
        session,
        manual_dispatch_text(
            order,
            reason=(
                f"⚠️ СДЭК не принял отправление: {escape(error)}\n"
                "Можно повторить кнопкой «Создать отправление СДЭК» в карточке заказа "
                "или оформить вручную:"
            ),
        ),
    )


# --- статусы ---


async def cdek_shipments_to_sync(session: AsyncSession) -> list[int]:
    """Живые отправления СДЭК — их статусы сверяем по расписанию."""
    result = await session.scalars(
        select(Shipment.id).where(
            Shipment.provider == DeliveryProviderCode.CDEK,
            Shipment.status.in_(ACTIVE_SHIPMENT_STATUSES),
        ),
    )
    return list(result)


async def shipment_id_by_uuid(session: AsyncSession, uuid: str) -> int | None:
    return await session.scalar(
        select(Shipment.id).where(
            Shipment.provider == DeliveryProviderCode.CDEK,
            Shipment.external_id == uuid,
        ),
    )


async def refresh(session: AsyncSession, shipment_id: int) -> Shipment | None:
    """Спросить СДЭК о заказе и отразить изменения у нас: номер, статус, этап заказа."""
    order_id = await session.scalar(select(Shipment.order_id).where(Shipment.id == shipment_id))
    if order_id is None:
        return None
    order = await orders_service.get_order(session, order_id)
    await orders_service.lock(session, order)
    shipment = await session.scalar(
        select(Shipment).where(Shipment.id == shipment_id).with_for_update(),
    )
    await session.refresh(shipment)
    if shipment.status not in ACTIVE_SHIPMENT_STATUSES:
        return shipment

    info = await cdek.get_order(shipment.external_id)

    if info.create_state == "INVALID":
        shipment.status = ShipmentStatus.CANCELLED
        shipment.status_raw = "СДЭК отклонил заказ"
        shipment.status_updated_at = now_utc()
        await session.flush()
        await _log_status(session, order, shipment, "INVALID")
        await report_create_failure(session, order, "; ".join(info.errors) or "без объяснения")
        return shipment

    if info.cdek_number and shipment.track_number != info.cdek_number:
        shipment.track_number = info.cdek_number
        await session.flush()
        await orders_service.log_event(
            session,
            order,
            OrderEventType.TRACK_ASSIGNED,
            payload={"provider": "cdek", "track": info.cdek_number},
        )
        await _report_created(session, order, info.cdek_number)

    if info.status_code is None:
        return shipment
    new_status = cdek.map_status(info.status_code)
    if info.status_name:
        shipment.status_raw = info.status_name[:128]
    if new_status == shipment.status:
        await session.flush()
        return shipment

    shipment.status = new_status
    shipment.status_updated_at = now_utc()
    await session.flush()
    await _log_status(session, order, shipment, info.status_code)
    await _follow_parcel(session, order, shipment)
    return shipment


async def _report_created(session: AsyncSession, order: Order, track: str) -> None:
    """Номер СДЭК получен — владелице: куда и с каким номером сдать посылку."""
    settings = get_settings().cdek
    where = f"<code>{escape(settings.shipment_point)}</code>"
    try:
        info = await cdek.point_info(settings.shipment_point)
        where = f"{escape(info.city)}, {escape(info.address)} (код {where})"
    except ExternalServiceError:
        pass
    notifications.schedule_owner_text(
        session,
        f"📦 <b>СДЭК: отправление по заказу {escape(order.admin_label)} создано</b>\n"
        f"Номер: <code>{escape(track)}</code>\n"
        f"Сдайте посылку в пункт {where} — назовите этот номер.\n\n"
        f"Получатель: {recipient_line(order)}\n"
        f"Пункт выдачи: {point_line(order)}",
    )


async def _log_status(
    session: AsyncSession,
    order: Order,
    shipment: Shipment,
    code: str,
) -> None:
    await orders_service.log_event(
        session,
        order,
        OrderEventType.SHIPMENT_STATUS_CHANGED,
        payload={
            "provider": str(shipment.provider),
            "status": shipment.status.value,
            "code": code,
            "name": shipment.status_raw or "",
        },
    )


async def _follow_parcel(session: AsyncSession, order: Order, shipment: Shipment) -> None:
    """Заказ идёт за посылкой: этапы и уведомления — как при ручной смене."""
    status = shipment.status
    if status == ShipmentStatus.IN_TRANSIT:
        await _advance(session, order, OrderStatus.SHIPPED, shipment)
    elif status == ShipmentStatus.ARRIVED:
        await _advance(session, order, OrderStatus.ARRIVED, shipment)
    elif status == ShipmentStatus.DELIVERED:
        await _advance(session, order, OrderStatus.COMPLETED, shipment)
    elif status == ShipmentStatus.RETURNED:
        notifications.schedule_owner_text(
            session,
            f"⚠️ СДЭК: посылку по заказу {escape(order.admin_label)} не вручили — "
            f"она возвращается. Номер <code>{escape(shipment.track_number or '')}</code>.",
        )
    elif status == ShipmentStatus.CANCELLED:
        notifications.schedule_owner_text(
            session,
            f"⚠️ СДЭК: отправление по заказу {escape(order.admin_label)} удалено. "
            "Если посылку ещё нужно отправить, создайте отправление заново в карточке заказа.",
        )


async def _advance(
    session: AsyncSession,
    order: Order,
    target: OrderStatus,
    shipment: Shipment,
) -> None:
    """Довести заказ до `target` по этапам доставки, ничего не перескакивая.

    Если заказ не на пути доставки (его вернули в сборку или отменили), этап не
    трогаем — только сообщаем в лог: решение за владелицей.
    """
    current = OrderStatus(order.status)
    if current not in _DELIVERY_PATH:
        logger.warning(
            "Отправление %s: заказ %s в «%s», этап по посылке не меняем",
            shipment.id,
            order.id,
            current,
        )
        return
    for step in _DELIVERY_PATH[_DELIVERY_PATH.index(current) + 1 :]:
        if _DELIVERY_PATH.index(step) > _DELIVERY_PATH.index(target):
            break
        try:
            if step == OrderStatus.ARRIVED:
                await orders_service.mark_arrived(session, order)
            else:
                await orders_service.change_status(session, order, step)
        except DomainError as error:
            logger.warning("Заказ %s: не перевести в «%s»: %s", order.id, step, error.message)
            return
        if step == OrderStatus.SHIPPED and shipment.track_number:
            notifications.schedule_customer_notification(
                session,
                order,
                MessageTemplateKey.TRACK_ASSIGNED,
                track=shipment.track_number,
            )
