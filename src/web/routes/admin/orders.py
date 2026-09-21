"""Список заказов и карточка заказа."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from core.clock import now_utc
from core.enums import OrderEventType, OrderStatus
from core.errors import DomainError, NotFoundError
from core.models import Customer, Order, Payment, Shipment
from core.services import board as board_service
from core.services import orders as orders_service
from core.services import settings as settings_service
from web.security import CurrentAdmin, DbSession, csrf_token, verify_csrf
from web.templating import render

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orders", tags=["admin"])

PAGE_SIZE = 25


@router.get("")
async def list_orders(
    request: Request,
    session: DbSession,
    admin: CurrentAdmin,
    q: Annotated[str, Query()] = "",
    order_status: Annotated[str, Query(alias="status")] = "",
    period: Annotated[str, Query()] = "",
    page: Annotated[int, Query(ge=1)] = 1,
):
    """Таблица всех заказов, включая неоплаченные и отменённые."""
    statement = (
        select(Order)
        .options(
            selectinload(Order.items),
            selectinload(Order.addons),
            selectinload(Order.customer),
        )
        .order_by(Order.created_at.desc())
    )

    query = q.strip()
    if query:
        pattern = f"%{query.lower()}%"
        statement = statement.join(Order.customer).where(
            or_(
                func.lower(Order.number).like(pattern),
                func.lower(Order.recipient_name).like(pattern),
                Order.recipient_phone.like(pattern),
                func.lower(Customer.username).like(pattern),
                func.lower(Customer.full_name).like(pattern),
            ),
        )
    if order_status:
        statement = statement.where(Order.status == order_status)

    since = _period_start(period)
    if since is not None:
        statement = statement.where(Order.created_at >= since)

    total = await session.scalar(
        select(func.count()).select_from(statement.order_by(None).subquery()),
    )
    rows = await session.scalars(statement.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE))

    return render(
        request,
        "admin/orders_list.html",
        {
            "orders": list(rows),
            "admin": admin,
            "q": query,
            "selected_status": order_status,
            "period": period,
            "statuses": list(OrderStatus),
            "page": page,
            "pages": max(1, ((total or 0) + PAGE_SIZE - 1) // PAGE_SIZE),
            "total": total or 0,
            "csrf_token": csrf_token(request),
        },
    )


@router.get("/{order_id}")
async def order_detail(
    request: Request,
    order_id: int,
    session: DbSession,
    admin: CurrentAdmin,
):
    order = await _get_order(session, order_id)
    events = await orders_service.list_events(session, order_id)

    payments = await session.scalars(
        select(Payment).where(Payment.order_id == order_id).order_by(Payment.created_at.desc()),
    )
    shipments = await session.scalars(
        select(Shipment).where(Shipment.order_id == order_id).order_by(Shipment.created_at.desc()),
    )

    calendar = await settings_service.get_working_calendar(session)
    ready_dates = await board_service.ready_dates_for_queue(session, calendar=calendar)
    ready_date = ready_dates.get(order.id)

    previous_orders = await session.scalar(
        select(func.count())
        .select_from(Order)
        .where(
            Order.customer_id == order.customer_id,
            Order.id != order.id,
            Order.status != OrderStatus.DRAFT,
        ),
    )

    return render(
        request,
        "admin/order_detail.html",
        {
            "order": order,
            "events": events,
            "payments": list(payments),
            "shipments": list(shipments),
            "ready_date": ready_date,
            "is_late": board_service.is_late(ready_date, order.promised_ready_date)
            if ready_date
            else False,
            "previous_orders": previous_orders or 0,
            "statuses": list(OrderStatus),
            "admin": admin,
            "csrf_token": csrf_token(request),
        },
    )


@router.post("/{order_id}/status")
async def change_status(
    request: Request,
    order_id: int,
    session: DbSession,
    admin: CurrentAdmin,
    new_status: Annotated[str, Form(alias="status")],
    csrf: Annotated[str, Form(alias="csrf_token")] = "",
):
    verify_csrf(request, csrf)
    order = await _get_order(session, order_id)
    try:
        await orders_service.change_status(
            session,
            order,
            OrderStatus(new_status),
            actor=orders_service.admin_actor(admin.id),
        )
    except (DomainError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=getattr(error, "message", "Такой переход недоступен"),
        ) from error
    return _back_to_order(order_id)


@router.post("/{order_id}/note")
async def save_note(
    request: Request,
    order_id: int,
    session: DbSession,
    admin: CurrentAdmin,
    note: Annotated[str, Form()] = "",
    csrf: Annotated[str, Form(alias="csrf_token")] = "",
):
    """Заметка для сборки. Клиент её не видит."""
    verify_csrf(request, csrf)
    order = await _get_order(session, order_id)
    order.admin_note = note.strip() or None
    await session.flush()
    await orders_service.log_event(
        session,
        order,
        OrderEventType.ADMIN_NOTE_CHANGED,
        actor=orders_service.admin_actor(admin.id),
    )
    return _back_to_order(order_id)


@router.post("/{order_id}/production-days")
async def save_production_days(
    request: Request,
    order_id: int,
    session: DbSession,
    admin: CurrentAdmin,
    production_days: Annotated[int, Form(ge=1, le=60)],
    csrf: Annotated[str, Form(alias="csrf_token")] = "",
):
    """Срок изготовления. Меняет расчётные даты всех следующих в очереди."""
    verify_csrf(request, csrf)
    order = await _get_order(session, order_id)
    was = order.production_days
    order.production_days = production_days
    await session.flush()
    await orders_service.log_event(
        session,
        order,
        OrderEventType.PRODUCTION_DAYS_CHANGED,
        payload={"from": was, "to": production_days},
        actor=orders_service.admin_actor(admin.id),
    )
    return _back_to_order(order_id)


@router.post("/{order_id}/promised-date")
async def save_promised_date(
    request: Request,
    order_id: int,
    session: DbSession,
    admin: CurrentAdmin,
    promised_date: Annotated[str, Form()] = "",
    csrf: Annotated[str, Form(alias="csrf_token")] = "",
):
    """Дата, обещанная клиенту. Сама не пересчитывается — только руками."""
    verify_csrf(request, csrf)
    order = await _get_order(session, order_id)
    was = order.promised_ready_date

    if promised_date.strip():
        try:
            order.promised_ready_date = dt.date.fromisoformat(promised_date)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Дата указана неверно",
            ) from None
    else:
        order.promised_ready_date = None

    await session.flush()
    await orders_service.log_event(
        session,
        order,
        OrderEventType.PROMISED_DATE_CHANGED,
        payload={
            "from": was.isoformat() if was else None,
            "to": order.promised_ready_date.isoformat() if order.promised_ready_date else None,
        },
        actor=orders_service.admin_actor(admin.id),
    )
    return _back_to_order(order_id)


@router.post("/{order_id}/cancel")
async def cancel_order(
    request: Request,
    order_id: int,
    session: DbSession,
    admin: CurrentAdmin,
    reason: Annotated[str, Form()] = "",
    csrf: Annotated[str, Form(alias="csrf_token")] = "",
):
    verify_csrf(request, csrf)
    order = await _get_order(session, order_id)
    try:
        await orders_service.cancel(
            session,
            order,
            actor=orders_service.admin_actor(admin.id),
            reason=reason.strip() or "отменён вручную",
        )
    except DomainError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=error.message,
        ) from error
    return _back_to_order(order_id)


async def _get_order(session, order_id: int) -> Order:
    try:
        return await orders_service.get_order(session, order_id)
    except NotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error.message,
        ) from error


def _back_to_order(order_id: int) -> RedirectResponse:
    return RedirectResponse(f"/admin/orders/{order_id}", status_code=status.HTTP_303_SEE_OTHER)


def _period_start(period: str) -> dt.datetime | None:
    """«сегодня», «неделя», «месяц» — в момент, с которого считаем."""
    now = now_utc()
    if period == "today":
        return now - dt.timedelta(days=1)
    if period == "week":
        return now - dt.timedelta(days=7)
    if period == "month":
        return now - dt.timedelta(days=30)
    return None
