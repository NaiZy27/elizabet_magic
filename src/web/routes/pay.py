"""Страница оплаты заказа.

Клиент приходит сюда по ссылке из бота. Пока платёжный провайдер не подключён,
работает внутренняя заглушка: оплата подтверждается кнопкой на этой же странице.
Место, куда встанет Робокасса, помечено ниже.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, status
from fastapi.responses import RedirectResponse

from core.config import get_settings
from core.enums import OrderStatus, PaymentProvider, PaymentStatus
from core.errors import DomainError, NotFoundError
from core.services import orders as orders_service
from core.services import payments as payments_service
from web.security import DbSession
from web.templating import render

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pay", tags=["pay"])


def _bot_url(suffix: str = "") -> str:
    """Ссылка «вернуться в бот»."""
    username = get_settings().bot.username.lstrip("@")
    if not username:
        return ""
    return f"https://t.me/{username}{suffix}"


@router.get("/{token}")
async def payment_page(request: Request, token: str, session: DbSession):
    """Сводка заказа и кнопка оплаты."""
    try:
        payment = await payments_service.get_by_token(session, token)
    except NotFoundError as error:
        return render(
            request,
            "pay/error.html",
            {"message": error.message},
            status_code=status.HTTP_404_NOT_FOUND,
        )

    order = await orders_service.get_order(session, payment.order_id)

    if payment.status == PaymentStatus.PAID:
        return RedirectResponse(f"/pay/{token}/success", status_code=status.HTTP_303_SEE_OTHER)

    if payment.status != PaymentStatus.PENDING or order.status != OrderStatus.WAITING_PAYMENT:
        return render(
            request,
            "pay/error.html",
            {"message": "Этот счёт больше не действует. Откройте заказ в боте."},
            status_code=status.HTTP_410_GONE,
        )

    return render(
        request,
        "pay/checkout.html",
        {
            "order": order,
            "payment": payment,
            "is_stub": payment.provider == PaymentProvider.STUB,
            "bot_url": _bot_url(),
        },
    )


@router.post("/{token}/confirm")
async def confirm_stub_payment(request: Request, token: str, session: DbSession):
    """Подтвердить оплату заглушкой.

    Доступно только провайдеру `stub`: когда магазин Робокассы подключён,
    статус меняет исключительно её уведомление с проверенной подписью.
    """
    payment = await payments_service.get_by_token(session, token)
    if payment.provider != PaymentProvider.STUB:
        return render(
            request,
            "pay/error.html",
            {"message": "Оплата подтверждается платёжной системой."},
            status_code=status.HTTP_403_FORBIDDEN,
        )

    try:
        await payments_service.confirm_payment(
            session,
            payment.id,
            amount_kopecks=payment.amount_kopecks,
            raw_result={"provider": "stub", "source": "pay page"},
        )
    except DomainError as error:
        return render(
            request,
            "pay/error.html",
            {"message": error.message},
            status_code=status.HTTP_409_CONFLICT,
        )

    return RedirectResponse(f"/pay/{token}/success", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/{token}/success")
async def payment_success(request: Request, token: str, session: DbSession):
    """«Оплата принята» с возвратом в бот."""
    try:
        payment = await payments_service.get_by_token(session, token)
    except NotFoundError as error:
        return render(
            request,
            "pay/error.html",
            {"message": error.message},
            status_code=status.HTTP_404_NOT_FOUND,
        )

    order = await orders_service.get_order(session, payment.order_id)
    return render(
        request,
        "pay/success.html",
        {
            "order": order,
            "paid": payment.status == PaymentStatus.PAID,
            "bot_url": _bot_url(f"?start=paid_{token}"),
        },
    )
