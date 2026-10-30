"""Возвраты: кому вернуть деньги и отметка «вернула».

Сам возврат владелица делает в кабинете Робокассы — здесь список, чтобы ничего
не потерялось. Раздел живёт во вкладках статистики, рядом с платежами.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse

from core.enums import RefundStatus
from core.errors import DomainError
from core.services import orders as orders_service
from core.services import refunds as refunds_service
from web.security import CurrentOwner, DbSession, csrf_token, verify_csrf
from web.templating import render

router = APIRouter(prefix="/refunds", tags=["admin"])


@router.get("")
async def refunds_page(request: Request, session: DbSession, admin: CurrentOwner):
    return render(
        request,
        "admin/refunds.html",
        {
            "pending": await refunds_service.list_refunds(session, RefundStatus.PENDING),
            "done": await refunds_service.list_refunds(session, RefundStatus.DONE, limit=50),
            "admin": admin,
            "csrf_token": csrf_token(request),
        },
    )


@router.post("/{refund_id}/done")
async def mark_done(
    request: Request,
    refund_id: int,
    session: DbSession,
    admin: CurrentOwner,
    note: Annotated[str, Form()] = "",
    csrf: Annotated[str, Form(alias="csrf_token")] = "",
):
    """Деньги вернули в кабинете Робокассы."""
    verify_csrf(request, csrf)
    try:
        await refunds_service.complete(
            session,
            refund_id,
            actor=orders_service.admin_actor(admin.id),
            note=note,
        )
    except DomainError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=error.message,
        ) from error
    return RedirectResponse("/admin/refunds", status_code=status.HTTP_303_SEE_OTHER)
