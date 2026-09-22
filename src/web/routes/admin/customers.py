"""Клиенты и платежи."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from core.db import folded
from core.enums import OrderStatus
from core.models import Customer, Order, Payment
from web.security import CurrentOwner, DbSession, csrf_token
from web.templating import render

router = APIRouter(tags=["admin"])

PAGE_SIZE = 30


@router.get("/customers")
async def customers_page(
    request: Request,
    session: DbSession,
    admin: CurrentOwner,
    q: Annotated[str, Query()] = "",
    page: Annotated[int, Query(ge=1)] = 1,
):
    """Список клиентов с числом заказов и суммой покупок."""
    paid_orders = (
        select(
            Order.customer_id.label("customer_id"),
            func.count(Order.id).label("orders_count"),
            func.coalesce(func.sum(Order.total_kopecks), 0).label("total_kopecks"),
            func.max(Order.created_at).label("last_order_at"),
        )
        .where(Order.paid_at.is_not(None))
        .group_by(Order.customer_id)
        .subquery()
    )

    statement = (
        select(
            Customer,
            func.coalesce(paid_orders.c.orders_count, 0),
            func.coalesce(paid_orders.c.total_kopecks, 0),
            paid_orders.c.last_order_at,
        )
        .outerjoin(paid_orders, paid_orders.c.customer_id == Customer.id)
        .order_by(paid_orders.c.last_order_at.desc().nulls_last(), Customer.id.desc())
    )

    query = q.strip()
    if query:
        pattern = f"%{query.lower()}%"
        conditions = [
            folded(Customer.full_name).like(pattern),
            folded(Customer.username).like(pattern),
            Customer.phone.like(pattern),
        ]
        if query.isdigit():
            conditions.append(Customer.telegram_user_id == int(query))
        statement = statement.where(or_(*conditions))

    total = await session.scalar(
        select(func.count()).select_from(statement.order_by(None).subquery()),
    )
    rows = (await session.execute(statement.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE))).all()

    return render(
        request,
        "admin/customers.html",
        {
            "rows": rows,
            "q": query,
            "page": page,
            "pages": max(1, ((total or 0) + PAGE_SIZE - 1) // PAGE_SIZE),
            "total": total or 0,
            "admin": admin,
            "csrf_token": csrf_token(request),
        },
    )


@router.get("/payments")
async def payments_page(
    request: Request,
    session: DbSession,
    admin: CurrentOwner,
    payment_status: Annotated[str, Query(alias="status")] = "",
    page: Annotated[int, Query(ge=1)] = 1,
):
    """Попытки оплаты: заказ, способ, сумма, состояние."""
    statement = (
        select(Payment)
        .options(selectinload(Payment.order).selectinload(Order.customer))
        .order_by(Payment.created_at.desc())
    )
    if payment_status:
        statement = statement.where(Payment.status == payment_status)

    total = await session.scalar(
        select(func.count()).select_from(statement.order_by(None).subquery()),
    )
    rows = await session.scalars(statement.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE))

    return render(
        request,
        "admin/payments.html",
        {
            "payments": list(rows),
            "selected_status": payment_status,
            "page": page,
            "pages": max(1, ((total or 0) + PAGE_SIZE - 1) // PAGE_SIZE),
            "total": total or 0,
            "draft_status": OrderStatus.DRAFT,
            "admin": admin,
            "csrf_token": csrf_token(request),
        },
    )
