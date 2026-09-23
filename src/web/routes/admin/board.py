"""Доска заказов: канбан-очередь и сохранение переносов."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from core.clock import format_date_short, today_moscow
from core.enums import BOARD_STATUSES, QUEUE_STATUSES, OrderStatus
from core.errors import ConflictError, DomainError
from core.services import board as board_service
from core.services import orders as orders_service
from core.services import settings as settings_service
from web.security import CurrentAdmin, CurrentAdminApi, DbSession, csrf_token, verify_csrf
from web.templating import render

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin"])
api_router = APIRouter(tags=["admin-api"])


@router.get("/board")
async def show_board(request: Request, session: DbSession, admin: CurrentAdmin):
    calendar = await settings_service.get_working_calendar(session)
    rules = await settings_service.get_order_rules(session)
    columns, metrics = await board_service.load_board(
        session,
        calendar=calendar,
        keep_completed_hours=rules.keep_completed_on_board_hours,
    )
    return render(
        request,
        "admin/board.html",
        {
            "columns": columns,
            "metrics": metrics,
            "admin": admin,
            "csrf_token": csrf_token(request),
            "queue_statuses": [item.value for item in QUEUE_STATUSES],
            "keep_completed_hours": rules.keep_completed_on_board_hours,
            "today": today_moscow(),
        },
    )


class BoardMove(BaseModel):
    """Что прислала доска после переноса карточки."""

    order_id: int
    status: str
    #: Вся очередь в видимом порядке: сначала «Собирается», затем «В очереди».
    #: Пустой список — карточку перенесли в колонку вне очереди.
    queue_ids: list[int] = Field(default_factory=list)


@api_router.post("/board/move")
async def move_card(
    request: Request,
    payload: BoardMove,
    session: DbSession,
    admin: CurrentAdminApi,
):
    """Перенос карточки: смена этапа и новый порядок очереди — одной транзакцией."""
    verify_csrf(request, request.headers.get("X-CSRF-Token"))

    try:
        new_status = OrderStatus(payload.status)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Неизвестная колонка",
        ) from None
    if new_status not in BOARD_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="В эту колонку переносить нельзя",
        )

    order = await orders_service.get_order(session, payload.order_id)
    actor = orders_service.admin_actor(admin.id)

    try:
        if order.status != new_status:
            await orders_service.change_status(session, order, new_status, actor=actor)
        if payload.queue_ids:
            await board_service.reorder_queue(session, payload.queue_ids)
    except ConflictError as error:
        # Доска устарела: кто-то уже поменял очередь в другом окне.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=error.message) from error
    except DomainError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=error.message,
        ) from error

    calendar = await settings_service.get_working_calendar(session)
    queue = await board_service.load_queue(session)
    ready_dates = board_service.compute_ready_dates(
        board_service.queue_entries(queue),
        calendar=calendar,
    )

    return {
        "positions": {str(item.id): item.queue_position for item in queue},
        "ready_dates": {
            str(order_id): format_date_short(date) for order_id, date in ready_dates.items()
        },
        "late": {
            str(item.id): board_service.is_late(
                ready_dates[item.id],
                item.promised_ready_date,
            )
            for item in queue
            if item.id in ready_dates
        },
    }
