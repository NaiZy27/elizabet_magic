"""Страница оплаты заказа и уведомления Робокассы.

Клиент приходит сюда по ссылке из бота, видит состав заказа построчно и уходит
платить в Робокассу. Оплату проводит только уведомление Робокассы на ResultURL
с проверенной подписью. С PAYMENTS_MODE=stub вместо Робокассы — кнопка-заглушка.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, status
from fastapi.responses import PlainTextResponse, RedirectResponse

from core.config import get_settings
from core.enums import OrderStatus, PaymentProvider, PaymentStatus
from core.errors import DomainError, NotFoundError
from core.models import Payment
from core.services import orders as orders_service
from core.services import payments as payments_service
from core.services import robokassa
from web.security import DbSession
from web.templating import render

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pay", tags=["pay"])
webhooks = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _bot_url(suffix: str = "") -> str:
    """Ссылка «вернуться в бот»."""
    username = get_settings().bot.username.lstrip("@")
    if not username:
        return ""
    return f"https://t.me/{username}{suffix}"


# --- Робокасса. Эти адреса объявлены раньше /{token}, иначе «robokassa» сочтётся токеном. ---


async def _form(request: Request) -> dict[str, str]:
    """Параметры Робокассы: она шлёт их POST-формой или в адресе — смотря как настроено."""
    values = {key: str(value) for key, value in request.query_params.items()}
    if request.method == "POST":
        form = await request.form()
        values.update({key: str(value) for key, value in form.items()})
    return values


@webhooks.api_route("/robokassa/result", methods=["GET", "POST"])
async def robokassa_result(request: Request, session: DbSession):
    """ResultURL: Робокасса сообщает об оплате. Ответ «OK<InvId>» — «принято»."""
    data = await _form(request)
    out_sum = data.get("OutSum", "")
    inv_id = data.get("InvId", "")
    if not robokassa.check_result_signature(out_sum, inv_id, data.get("SignatureValue", "")):
        logger.warning("Робокасса: неверная подпись уведомления по счёту %r", inv_id)
        return PlainTextResponse("bad sign", status_code=status.HTTP_400_BAD_REQUEST)
    if not inv_id.isdigit():
        return PlainTextResponse("bad invoice", status_code=status.HTTP_400_BAD_REQUEST)

    raw = {key: value for key, value in data.items() if key != "SignatureValue"}
    fee = data.get("Fee")
    try:
        await payments_service.confirm_payment(
            session,
            int(inv_id),
            amount_kopecks=robokassa.parse_out_sum(out_sum),
            external_id=inv_id,
            raw_result=raw,
            fee_kopecks=robokassa.parse_out_sum(fee) if fee else None,
        )
    except NotFoundError:
        logger.error("Робокасса прислала оплату по неизвестному счёту %s", inv_id)
        return PlainTextResponse("unknown invoice", status_code=status.HTTP_404_NOT_FOUND)
    except DomainError as error:
        # Сумма не совпала — это не наш платёж в том виде, в каком мы его выставили.
        logger.error("Робокасса, счёт %s: %s", inv_id, error.message)
        return PlainTextResponse("rejected", status_code=status.HTTP_409_CONFLICT)
    return PlainTextResponse(f"OK{inv_id}")


async def _payment_by_inv_id(session, inv_id: str):
    if not inv_id.isdigit():
        return None
    return await session.get(Payment, int(inv_id))


@router.api_route("/robokassa/success", methods=["GET", "POST"])
async def robokassa_success(request: Request, session: DbSession):
    """SuccessURL: клиент вернулся после оплаты. Сама оплата проводится ResultURL."""
    data = await _form(request)
    payment = await _payment_by_inv_id(session, data.get("InvId", ""))
    if payment is None:
        return render(
            request,
            "pay/error.html",
            {"message": "Оплата принята. Вернитесь в бот — там будет номер заказа."},
        )
    return RedirectResponse(
        f"/pay/{payment.public_token}/success", status_code=status.HTTP_303_SEE_OTHER
    )


@router.api_route("/robokassa/fail", methods=["GET", "POST"])
async def robokassa_fail(request: Request, session: DbSession):
    """FailURL: клиент отказался от оплаты или она не прошла — вернём к заказу."""
    data = await _form(request)
    payment = await _payment_by_inv_id(session, data.get("InvId", ""))
    if payment is None:
        return render(
            request,
            "pay/error.html",
            {"message": "Оплата не прошла. Откройте заказ в боте и попробуйте ещё раз."},
        )
    return RedirectResponse(f"/pay/{payment.public_token}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/{token}/go")
async def go_to_robokassa(request: Request, token: str, session: DbSession):
    """Кнопка «Оплатить»: собираем подписанную ссылку и отправляем клиента в Робокассу."""
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
    if (
        payment.provider != PaymentProvider.ROBOKASSA
        or payment.status != PaymentStatus.PENDING
        or order.status != OrderStatus.WAITING_PAYMENT
    ):
        return RedirectResponse(f"/pay/{token}", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse(
        robokassa.payment_link(payment, order), status_code=status.HTTP_303_SEE_OTHER
    )


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
