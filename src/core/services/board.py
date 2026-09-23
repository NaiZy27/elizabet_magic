"""Очередь сборки: место заказа в общей очереди.

Очередь одна на всю мастерскую и живёт в поле orders.queue_position. Заказы
собираются по очереди: каждый занимает свои рабочие дни, следующий начинается
после предыдущего. Поэтому даты готовности не хранятся, а считаются при чтении —
перестановка карточки на доске сразу сдвигает даты у всех, кто идёт следом.

Календарная математика — в core.workdays, здесь только работа с базой.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.clock import now_utc, today_moscow
from core.enums import BOARD_STATUSES, QUEUE_STATUSES, OrderStatus
from core.errors import ConflictError
from core.labels import ORDER_STATUS_LABELS
from core.models import Order, OrderItem
from core.workdays import (
    QueueEntry,
    WorkingCalendar,
    compute_ready_dates,
    is_late,
    next_working_day,
)

__all__ = [
    "BoardCard",
    "BoardColumn",
    "BoardMetrics",
    "QueueEntry",
    "WorkingCalendar",
    "append_to_queue",
    "compute_ready_dates",
    "is_late",
    "load_board",
    "load_queue",
    "queue_entries",
    "ready_dates_for_queue",
    "remove_from_queue",
    "reorder_queue",
]


@dataclass(frozen=True, slots=True)
class BoardCard:
    """Карточка на доске вместе со всем, что на ней показывается."""

    order: Order
    position: int | None
    ready_date: dt.date | None
    is_late: bool


@dataclass(frozen=True, slots=True)
class BoardColumn:
    status: OrderStatus
    label: str
    cards: list[BoardCard]

    @property
    def count(self) -> int:
        return len(self.cards)


@dataclass(frozen=True, slots=True)
class BoardMetrics:
    """Сводка над доской: что видно утром, не открывая заказы."""

    queued: int
    ready_today: int
    ready_tomorrow: int
    ready_to_ship: int
    month_revenue_kopecks: int


async def load_board(
    session: AsyncSession,
    *,
    calendar: WorkingCalendar,
    today: dt.date | None = None,
    keep_completed_hours: int = 24,
) -> tuple[list[BoardColumn], BoardMetrics]:
    """Колонки доски и метрики над ней.

    Полученные заказы висят на доске `keep_completed_hours` часов и уходят с неё —
    из базы они никуда не деваются, их видно в списке заказов и в статистике.
    """
    day = today or today_moscow()
    hide_completed_before = now_utc() - dt.timedelta(hours=keep_completed_hours)
    result = await session.scalars(
        select(Order)
        .options(
            selectinload(Order.items).selectinload(OrderItem.addons),
            selectinload(Order.addons),
            selectinload(Order.customer),
        )
        .where(
            Order.status.in_(BOARD_STATUSES),
            (Order.status != OrderStatus.COMPLETED)
            | (Order.status_changed_at >= hide_completed_before),
        )
        .order_by(Order.status_changed_at.desc()),
    )
    orders = list(result)
    ready_dates = compute_ready_dates(queue_entries(orders), calendar=calendar, today=day)

    columns: list[BoardColumn] = []
    for status in BOARD_STATUSES:
        cards = [
            BoardCard(
                order=order,
                position=order.queue_position,
                ready_date=ready_dates.get(order.id),
                is_late=is_late(ready_dates[order.id], order.promised_ready_date)
                if order.id in ready_dates
                else False,
            )
            for order in orders
            if order.status == status
        ]
        if status in QUEUE_STATUSES:
            # В очереди порядок задаёт владелица, в остальных колонках — время перехода.
            cards.sort(key=lambda card: card.position or 0)
        columns.append(BoardColumn(status=status, label=ORDER_STATUS_LABELS[status], cards=cards))

    month_start = day.replace(day=1)
    revenue = await session.scalar(
        select(func.coalesce(func.sum(Order.total_kopecks), 0)).where(
            Order.paid_at.is_not(None),
            Order.refunded_at.is_(None),
            func.date(func.timezone("Europe/Moscow", Order.paid_at)) >= month_start,
        ),
    )

    tomorrow = next_working_day(day, calendar)
    metrics = BoardMetrics(
        queued=sum(1 for order in orders if order.status == OrderStatus.QUEUED),
        ready_today=sum(1 for date in ready_dates.values() if date == day),
        ready_tomorrow=sum(1 for date in ready_dates.values() if date == tomorrow),
        ready_to_ship=sum(1 for order in orders if order.status == OrderStatus.READY),
        month_revenue_kopecks=int(revenue or 0),
    )
    return columns, metrics


async def load_queue(session: AsyncSession, *, for_update: bool = False) -> list[Order]:
    """Заказы очереди по возрастанию позиции.

    С for_update строки блокируются до конца транзакции: перестановка карточек и
    постановка оплаченного заказа в конец не должны выполняться параллельно.
    """
    statement = (
        select(Order).where(Order.status.in_(tuple(QUEUE_STATUSES))).order_by(Order.queue_position)
    )
    if for_update:
        statement = statement.with_for_update()
    result = await session.scalars(statement)
    return list(result)


def queue_entries(orders: Iterable[Order]) -> list[QueueEntry]:
    """Заказы -> записи для расчёта дат. Без позиции в очереди заказ не участвует."""
    entries: list[QueueEntry] = []
    for order in orders:
        if order.queue_position is None:
            continue
        entries.append(
            QueueEntry(
                order_id=order.id,
                production_days=order.production_days,
                status=OrderStatus(order.status),
                queue_position=order.queue_position,
            ),
        )
    return entries


async def ready_dates_for_queue(
    session: AsyncSession,
    *,
    calendar: WorkingCalendar,
    today: dt.date | None = None,
) -> dict[int, dt.date]:
    """Расчётные даты готовности для всех заказов в очереди."""
    queue = await load_queue(session)
    return compute_ready_dates(queue_entries(queue), calendar=calendar, today=today)


async def append_to_queue(session: AsyncSession, order: Order) -> int:
    """Поставить оплаченный заказ в конец очереди и вернуть его позицию."""
    queue = await load_queue(session, for_update=True)
    taken = [item.queue_position for item in queue if item.queue_position is not None]
    position = (max(taken) + 1) if taken else 1
    order.queue_position = position
    await session.flush()
    return position


async def remove_from_queue(session: AsyncSession, order: Order) -> None:
    """Убрать заказ из очереди и сомкнуть номера, чтобы не осталось дыр."""
    if order.queue_position is None:
        return
    queue = await load_queue(session, for_update=True)
    order.queue_position = None
    remaining = [item for item in queue if item.id != order.id and item.queue_position is not None]
    _renumber(remaining)
    await session.flush()


async def reorder_queue(session: AsyncSession, ordered_ids: Sequence[int]) -> list[Order]:
    """Переписать очередь в присланном порядке.

    Порядок приходит с доски целиком: сначала колонка «Собирается», затем «В очереди».
    Если в базе очередь уже другая — значит, её успели поменять, и доска устарела.
    """
    queue = await load_queue(session, for_update=True)
    current_ids = {order.id for order in queue}
    if len(set(ordered_ids)) != len(ordered_ids) or current_ids != set(ordered_ids):
        raise ConflictError("Доска устарела: очередь уже изменилась")

    by_id = {order.id: order for order in queue}
    ordered = [by_id[order_id] for order_id in ordered_ids]
    _renumber(ordered)
    await session.flush()
    return ordered


def _renumber(orders: Sequence[Order]) -> None:
    """Проставить позиции 1..N в текущем порядке списка.

    В процессе позиции ненадолго совпадают, но уникальность в базе отложена
    до конца транзакции — так и задумано.
    """
    for position, order in enumerate(orders, start=1):
        order.queue_position = position
