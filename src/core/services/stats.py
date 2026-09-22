"""Базовая статистика за период.

Считаем только по оплаченным заказам: черновики, неоплаченные и возвращённые
в выручку не попадают.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.clock import today_moscow
from core.enums import VIDEO_ADDON_CODE, OrderStatus
from core.models import Order, OrderAddon, OrderItem

#: Периоды, которые можно выбрать в админке.
PERIOD_LABELS: dict[str, str] = {
    "today": "Сегодня",
    "week": "Неделя",
    "month": "Месяц",
    "custom": "Свой период",
}


@dataclass(frozen=True, slots=True)
class TopProduct:
    name: str
    quantity: int
    revenue_kopecks: int


@dataclass(frozen=True, slots=True)
class Stats:
    """Сводка за период."""

    since: dt.date
    until: dt.date
    paid_orders: int = 0
    revenue_kopecks: int = 0
    revenue_without_delivery_kopecks: int = 0
    average_check_kopecks: int = 0
    cancelled_orders: int = 0
    orders_with_video: int = 0
    repeat_customers: int = 0
    unique_customers: int = 0
    top_products: list[TopProduct] = field(default_factory=list)
    by_delivery_provider: dict[str, int] = field(default_factory=dict)

    @property
    def video_share(self) -> int:
        """Доля заказов с видео, в процентах."""
        if not self.paid_orders:
            return 0
        return round(self.orders_with_video * 100 / self.paid_orders)


def period_range(
    period: str, *, since: dt.date | None, until: dt.date | None
) -> tuple[dt.date, dt.date]:
    """Границы периода по выбору в админке."""
    today = today_moscow()
    if period == "today":
        return today, today
    if period == "week":
        return today - dt.timedelta(days=6), today
    if period == "custom" and since and until:
        return (since, until) if since <= until else (until, since)
    return today - dt.timedelta(days=29), today


async def collect(
    session: AsyncSession,
    *,
    since: dt.date,
    until: dt.date,
) -> Stats:
    """Собрать метрики за период по дате оплаты."""
    # Заказ попадает в период по моменту оплаты — по нему считается выручка.
    paid_at_date = func.date(func.timezone("Europe/Moscow", Order.paid_at))
    in_period = (
        Order.paid_at.is_not(None),
        Order.refunded_at.is_(None),
        paid_at_date >= since,
        paid_at_date <= until,
    )

    totals = (
        await session.execute(
            select(
                func.count(Order.id),
                func.coalesce(func.sum(Order.total_kopecks), 0),
                func.coalesce(func.sum(Order.delivery_kopecks), 0),
                func.count(func.distinct(Order.customer_id)),
            ).where(*in_period),
        )
    ).one()
    paid_orders, revenue, delivery, unique_customers = totals

    cancelled_at_date = func.date(func.timezone("Europe/Moscow", Order.cancelled_at))
    cancelled = await session.scalar(
        select(func.count(Order.id)).where(
            Order.status == OrderStatus.CANCELLED,
            Order.cancelled_at.is_not(None),
            cancelled_at_date >= since,
            cancelled_at_date <= until,
        ),
    )

    with_video = await session.scalar(
        select(func.count(func.distinct(Order.id)))
        .join(OrderAddon, OrderAddon.order_id == Order.id)
        .where(*in_period, OrderAddon.code_snapshot == VIDEO_ADDON_CODE),
    )

    # Популярность считаем по боксам, а не по заказам: в одном заказе их может быть несколько.
    top_rows = (
        await session.execute(
            select(
                OrderItem.name_snapshot,
                func.sum(OrderItem.quantity),
                func.sum(OrderItem.quantity * OrderItem.unit_price_kopecks),
            )
            .join(Order, Order.id == OrderItem.order_id)
            .where(*in_period)
            .group_by(OrderItem.name_snapshot)
            .order_by(func.sum(OrderItem.quantity).desc())
            .limit(5),
        )
    ).all()

    provider_rows = (
        await session.execute(
            select(Order.delivery_provider, func.count(Order.id))
            .where(*in_period)
            .group_by(Order.delivery_provider),
        )
    ).all()

    # Повторные — те, у кого за всё время больше одного оплаченного заказа.
    repeat_customers = await session.scalar(
        select(func.count()).select_from(
            select(Order.customer_id)
            .where(Order.paid_at.is_not(None), Order.refunded_at.is_(None))
            .group_by(Order.customer_id)
            .having(func.count(Order.id) > 1)
            .subquery(),
        ),
    )

    paid_orders = int(paid_orders or 0)
    revenue = int(revenue or 0)
    delivery = int(delivery or 0)

    return Stats(
        since=since,
        until=until,
        paid_orders=paid_orders,
        revenue_kopecks=revenue,
        revenue_without_delivery_kopecks=revenue - delivery,
        average_check_kopecks=round(revenue / paid_orders) if paid_orders else 0,
        cancelled_orders=int(cancelled or 0),
        orders_with_video=int(with_video or 0),
        repeat_customers=int(repeat_customers or 0),
        unique_customers=int(unique_customers or 0),
        top_products=[
            TopProduct(name=name, quantity=int(quantity or 0), revenue_kopecks=int(amount or 0))
            for name, quantity, amount in top_rows
        ],
        by_delivery_provider={
            (provider or "не выбрана"): int(count or 0) for provider, count in provider_rows
        },
    )
