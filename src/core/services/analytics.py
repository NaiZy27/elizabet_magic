"""Аналитика для панели: загрузка мастерской по дням, динамика продаж, воронка бота.

Только чтение. Деньги и заказы считаются так же, как в core.services.stats:
по оплаченным заказам без возвратов, день — по московскому времени оплаты.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.clock import today_moscow
from core.enums import OrderEventType
from core.models import Order, OrderEvent, OrderItem, ProductVariant
from core.services import board as board_service
from core.workdays import WorkingCalendar, ensure_working_day, next_working_day

MOSCOW = "Europe/Moscow"


# --- загрузка мастерской ---


@dataclass(slots=True)
class PlannedOrder:
    id: int
    label: str
    name: str
    video_boxes: int
    plain_boxes: int


@dataclass(slots=True)
class DayLoad:
    """Один день календаря сборки."""

    day: dt.date
    is_working: bool
    #: Занято единиц из дневного лимита (бокс с видео «весит» больше обычного).
    used_units: int = 0
    limit_units: int = 0
    video_boxes: int = 0
    plain_boxes: int = 0
    #: Заказы, которые в этот день будут готовы.
    ready: list[PlannedOrder] = field(default_factory=list)

    @property
    def boxes(self) -> int:
        return self.video_boxes + self.plain_boxes

    @property
    def percent(self) -> int:
        if not self.limit_units:
            return 0
        return min(100, round(self.used_units * 100 / self.limit_units))

    @property
    def is_full(self) -> bool:
        return self.is_working and self.limit_units > 0 and self.used_units >= self.limit_units


@dataclass(slots=True)
class Plan:
    days: list[DayLoad]
    #: Ближайший день, когда соберём новый бокс без видео / с видео, если оплатят сейчас.
    next_free_plain: dt.date | None
    next_free_video: dt.date | None
    queue_orders: int
    queue_boxes: int
    queue_video_boxes: int


async def production_plan(
    session: AsyncSession,
    *,
    calendar: WorkingCalendar,
    days: int = 35,
    start: dt.date | None = None,
    today: dt.date | None = None,
) -> Plan:
    """Разложить очередь по дням так же, как это делает расчёт дат готовности.

    Возвращает `days` календарных дней начиная со `start` (по умолчанию — с сегодня).
    """
    today = today or today_moscow()
    start = start or today
    queue = await board_service.load_queue(session)
    entries = sorted(board_service.queue_entries(queue), key=lambda item: item.queue_position)
    by_id = {order.id: order for order in queue}

    loads: dict[dt.date, DayLoad] = {}

    def day_load(day: dt.date) -> DayLoad:
        if day not in loads:
            loads[day] = DayLoad(day=day, is_working=calendar.is_working(day))
        return loads[day]

    capacity = calendar.capacity
    limit = capacity.day_units if capacity else 0
    cursor = ensure_working_day(today, calendar)
    used = 0

    for entry in entries:
        boxes = [True] * entry.video_boxes + [False] * entry.plain_boxes or [False]
        if capacity is None:
            # Без лимита заказ просто занимает свои рабочие дни после предыдущего.
            for _ in range(max(1, entry.production_days) - 1):
                cursor = next_working_day(cursor, calendar)
            load = day_load(cursor)
            load.video_boxes += entry.video_boxes
            load.plain_boxes += entry.plain_boxes
            ready_day = cursor
            cursor = next_working_day(cursor, calendar)
        else:
            for with_video in boxes:
                units = capacity.box_units(with_video=with_video)
                if used + units > limit:
                    cursor = next_working_day(cursor, calendar)
                    used = 0
                used += units
                load = day_load(cursor)
                load.used_units += units
                if with_video:
                    load.video_boxes += 1
                else:
                    load.plain_boxes += 1
            ready_day = cursor
        order = by_id[entry.order_id]
        day_load(ready_day).ready.append(
            PlannedOrder(
                id=order.id,
                label=order.admin_label,
                name=order.recipient_name or "",
                video_boxes=entry.video_boxes,
                plain_boxes=entry.plain_boxes,
            ),
        )

    def next_free(with_video: bool) -> dt.date | None:
        if capacity is None:
            return cursor
        units = capacity.box_units(with_video=with_video)
        return cursor if used + units <= limit else next_working_day(cursor, calendar)

    result: list[DayLoad] = []
    for offset in range(days):
        day = start + dt.timedelta(days=offset)
        load = day_load(day)
        load.limit_units = limit if load.is_working else 0
        result.append(load)

    return Plan(
        days=result,
        next_free_plain=next_free(False),
        next_free_video=next_free(True),
        queue_orders=len(entries),
        queue_boxes=sum(entry.video_boxes + entry.plain_boxes for entry in entries),
        queue_video_boxes=sum(entry.video_boxes for entry in entries),
    )


# --- продажи ---


def _paid_in(since: dt.date, until: dt.date):
    paid_day = func.date(func.timezone(MOSCOW, Order.paid_at))
    return (
        Order.paid_at.is_not(None),
        Order.refunded_at.is_(None),
        paid_day >= since,
        paid_day <= until,
    ), paid_day


@dataclass(slots=True)
class DayPoint:
    day: dt.date
    revenue_kopecks: int = 0
    orders: int = 0


@dataclass(slots=True)
class Funnel:
    started: int
    submitted: int
    paid: int


@dataclass(slots=True)
class SalesTrend:
    days: list[DayPoint]
    weekdays: list[int]
    boxes_sold: int
    previous_revenue_kopecks: int
    previous_orders: int
    previous_average_kopecks: int
    new_customers: int
    returning_customers: int
    funnel: Funnel


def _delta(current: int, previous: int) -> int | None:
    """Изменение к прошлому периоду в процентах. None — сравнивать не с чем."""
    if not previous:
        return None
    return round((current - previous) * 100 / previous)


async def sales_trend(session: AsyncSession, *, since: dt.date, until: dt.date) -> SalesTrend:
    in_period, paid_day = _paid_in(since, until)

    rows = (
        await session.execute(
            select(paid_day, func.count(Order.id), func.coalesce(func.sum(Order.total_kopecks), 0))
            .where(*in_period)
            .group_by(paid_day),
        )
    ).all()
    by_day = {day: (int(count), int(amount)) for day, count, amount in rows}
    points = []
    span = (until - since).days + 1
    for offset in range(span):
        day = since + dt.timedelta(days=offset)
        count, amount = by_day.get(day, (0, 0))
        points.append(DayPoint(day=day, revenue_kopecks=amount, orders=count))

    weekdays = [0] * 7
    for day, (count, _amount) in by_day.items():
        weekdays[day.weekday()] += count

    boxes_sold = await session.scalar(
        select(func.coalesce(func.sum(OrderItem.quantity), 0))
        .join(Order, Order.id == OrderItem.order_id)
        .where(*in_period),
    )

    # Прошлый период той же длины — для «+12% к прошлой неделе».
    prev_until = since - dt.timedelta(days=1)
    prev_since = prev_until - dt.timedelta(days=span - 1)
    prev_filter, _ = _paid_in(prev_since, prev_until)
    prev_orders, prev_revenue = (
        await session.execute(
            select(func.count(Order.id), func.coalesce(func.sum(Order.total_kopecks), 0)).where(
                *prev_filter,
            ),
        )
    ).one()
    prev_orders = int(prev_orders or 0)
    prev_revenue = int(prev_revenue or 0)

    # Новые — те, чей первый оплаченный заказ пришёлся на период.
    first_paid = (
        select(Order.customer_id, func.min(Order.paid_at).label("first_paid"))
        .where(Order.paid_at.is_not(None), Order.refunded_at.is_(None))
        .group_by(Order.customer_id)
        .subquery()
    )
    first_day = func.date(func.timezone(MOSCOW, first_paid.c.first_paid))
    new_customers = await session.scalar(
        select(func.count()).select_from(first_paid).where(first_day >= since, first_day <= until),
    )
    buyers = await session.scalar(
        select(func.count(func.distinct(Order.customer_id))).where(*in_period),
    )
    new_customers = int(new_customers or 0)
    buyers = int(buyers or 0)

    # Воронка бота по событиям заказа: начали оформлять → дошли до оплаты → оплатили.
    event_day = func.date(func.timezone(MOSCOW, OrderEvent.created_at))

    async def count_events(kind: OrderEventType) -> int:
        value = await session.scalar(
            select(func.count(func.distinct(OrderEvent.order_id))).where(
                OrderEvent.event_type == kind,
                event_day >= since,
                event_day <= until,
            ),
        )
        return int(value or 0)

    paid_orders = sum(point.orders for point in points)
    funnel = Funnel(
        started=await count_events(OrderEventType.CREATED),
        submitted=await count_events(OrderEventType.SUBMITTED),
        paid=paid_orders,
    )

    return SalesTrend(
        days=points,
        weekdays=weekdays,
        boxes_sold=int(boxes_sold or 0),
        previous_revenue_kopecks=prev_revenue,
        previous_orders=prev_orders,
        previous_average_kopecks=round(prev_revenue / prev_orders) if prev_orders else 0,
        new_customers=new_customers,
        returning_customers=max(0, buyers - new_customers),
        funnel=funnel,
    )


delta = _delta


async def product_sales(session: AsyncSession, *, days: int = 30) -> dict[int, int]:
    """Сколько боксов каждого товара продано за последние `days` дней: product_id -> штук."""
    until = today_moscow()
    since = until - dt.timedelta(days=days - 1)
    in_period, _ = _paid_in(since, until)
    rows = (
        await session.execute(
            select(ProductVariant.product_id, func.sum(OrderItem.quantity))
            .join(OrderItem, OrderItem.variant_id == ProductVariant.id)
            .join(Order, Order.id == OrderItem.order_id)
            .where(*in_period)
            .group_by(ProductVariant.product_id),
        )
    ).all()
    return {int(product_id): int(count or 0) for product_id, count in rows}


# --- клиенты ---


@dataclass(slots=True)
class CustomerSummary:
    buyers: int
    repeat: int
    average_ltv_kopecks: int
    new_30d: int

    @property
    def repeat_percent(self) -> int:
        return round(self.repeat * 100 / self.buyers) if self.buyers else 0


async def customer_summary(session: AsyncSession) -> CustomerSummary:
    """Сводка по покупателям за всё время: сколько, сколько вернулись, сколько приносят."""
    per_customer = (
        select(
            Order.customer_id,
            func.count(Order.id).label("orders"),
            func.sum(Order.total_kopecks).label("spent"),
            func.min(Order.paid_at).label("first_paid"),
        )
        .where(Order.paid_at.is_not(None), Order.refunded_at.is_(None))
        .group_by(Order.customer_id)
        .subquery()
    )
    since = today_moscow() - dt.timedelta(days=29)
    first_day = func.date(func.timezone(MOSCOW, per_customer.c.first_paid))
    buyers, repeat, spent, new_30d = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(per_customer.c.orders > 1),
                func.coalesce(func.sum(per_customer.c.spent), 0),
                func.count().filter(first_day >= since),
            ).select_from(per_customer),
        )
    ).one()
    buyers = int(buyers or 0)
    return CustomerSummary(
        buyers=buyers,
        repeat=int(repeat or 0),
        average_ltv_kopecks=round(int(spent or 0) / buyers) if buyers else 0,
        new_30d=int(new_30d or 0),
    )
