"""Оплата заказа.

Пока подключён только внутренний провайдер-заглушка: оплата подтверждается со
страницы /pay вручную. Робокасса встанет на это же место — создание платежа,
идемпотентное подтверждение и переход заказа в очередь останутся без изменений.

Подтверждение оплаты идемпотентно: уведомление о платеже может прийти несколько раз,
но заказ должен быть оплачен ровно один раз.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.clock import now_utc
from core.config import get_settings
from core.enums import (
    OrderEventType,
    OrderStatus,
    PaymentProvider,
    PaymentStatus,
    ProviderEnvironment,
)
from core.errors import ConflictError, NotFoundError, ValidationError
from core.models import Order, Payment
from core.services import orders as orders_service


def current_provider() -> PaymentProvider:
    """Робокасса, если магазин настроен, иначе заглушка для разработки."""
    settings = get_settings()
    return PaymentProvider.ROBOKASSA if settings.robokassa.login else PaymentProvider.STUB


def current_environment() -> ProviderEnvironment:
    settings = get_settings()
    return (
        ProviderEnvironment.TEST if settings.robokassa.is_test else ProviderEnvironment.PRODUCTION
    )


def payment_url(payment: Payment) -> str:
    """Ссылка, которую бот даёт клиенту. Id платежа в адресе не светится."""
    base = get_settings().public_base_url
    return f"{base}/pay/{payment.public_token}"


async def create_payment(session: AsyncSession, order: Order) -> Payment:
    """Создать попытку оплаты заказа.

    Активная попытка может быть только одна: если клиент нажал «Оплатить» дважды,
    он получит ту же ссылку.
    """
    if order.status != OrderStatus.WAITING_PAYMENT:
        raise ConflictError("Оплатить можно только оформленный заказ")
    if order.total_kopecks <= 0:
        raise ValidationError("Сумма заказа должна быть больше нуля")

    existing = await get_pending_payment(session, order.id)
    if existing is not None:
        if existing.amount_kopecks == order.total_kopecks:
            return existing
        # Заказ изменился после создания ссылки — старая попытка больше не годится.
        existing.status = PaymentStatus.CANCELLED
        await session.flush()

    payment = Payment(
        order_id=order.id,
        provider=current_provider(),
        environment=current_environment(),
        public_token=uuid.uuid4(),
        amount_kopecks=order.total_kopecks,
        status=PaymentStatus.PENDING,
    )
    session.add(payment)
    await session.flush()

    await orders_service.log_event(
        session,
        order,
        OrderEventType.PAYMENT_CREATED,
        payload={"payment_id": payment.id, "amount_kopecks": payment.amount_kopecks},
        actor=orders_service.ACTOR_BOT,
    )
    return payment


async def get_pending_payment(session: AsyncSession, order_id: int) -> Payment | None:
    return await session.scalar(
        select(Payment).where(
            Payment.order_id == order_id,
            Payment.status == PaymentStatus.PENDING,
        ),
    )


async def get_by_token(session: AsyncSession, token: str | uuid.UUID) -> Payment:
    """Платёж по токену из ссылки."""
    try:
        token_value = uuid.UUID(str(token))
    except ValueError:
        raise NotFoundError("Ссылка на оплату неверна") from None

    payment = await session.scalar(select(Payment).where(Payment.public_token == token_value))
    if payment is None:
        raise NotFoundError("Ссылка на оплату устарела")
    return payment


async def confirm_payment(
    session: AsyncSession,
    payment_id: int,
    *,
    amount_kopecks: int | None = None,
    external_id: str | None = None,
    raw_result: dict[str, Any] | None = None,
    fee_kopecks: int | None = None,
    actor: str = orders_service.ACTOR_SYSTEM,
) -> Payment:
    """Отметить оплату успешной и отправить заказ в очередь.

    Повторный вызов с тем же платежом ничего не меняет и не шлёт второе уведомление.
    """
    # Блокируем строку: два одновременных уведомления не должны оплатить заказ дважды.
    payment = await session.scalar(
        select(Payment).where(Payment.id == payment_id).with_for_update(),
    )
    if payment is None:
        raise NotFoundError("Платёж не найден")

    if payment.status == PaymentStatus.PAID:
        return payment
    if payment.status != PaymentStatus.PENDING:
        raise ConflictError(f"Платёж уже в состоянии «{payment.status}»")
    if amount_kopecks is not None and amount_kopecks != payment.amount_kopecks:
        raise ValidationError("Оплаченная сумма не совпадает с суммой заказа")

    payment.status = PaymentStatus.PAID
    payment.paid_at = now_utc()
    payment.external_id = external_id
    payment.raw_result = raw_result
    payment.fee_kopecks = fee_kopecks
    await session.flush()

    order = await orders_service.get_order(session, payment.order_id)
    await orders_service.mark_paid(session, order, actor=actor)
    return payment


async def fail_payment(
    session: AsyncSession,
    payment_id: int,
    *,
    raw_result: dict[str, Any] | None = None,
    reason: str | None = None,
) -> Payment:
    """Отметить попытку неуспешной. Заказ остаётся в «Ожидает оплаты»."""
    payment = await session.get(Payment, payment_id)
    if payment is None:
        raise NotFoundError("Платёж не найден")
    if payment.status != PaymentStatus.PENDING:
        return payment

    payment.status = PaymentStatus.FAILED
    payment.raw_result = raw_result
    await session.flush()

    order = await orders_service.get_order(session, payment.order_id)
    await orders_service.log_event(
        session,
        order,
        OrderEventType.PAYMENT_FAILED,
        payload={"payment_id": payment.id, "reason": reason or ""},
    )
    return payment


async def cancel_pending(session: AsyncSession, order_id: int) -> None:
    """Закрыть активную попытку оплаты — например, при отмене заказа."""
    payment = await get_pending_payment(session, order_id)
    if payment is not None:
        payment.status = PaymentStatus.CANCELLED
        await session.flush()


async def list_payments(session: AsyncSession, order_id: int) -> list[Payment]:
    result = await session.scalars(
        select(Payment).where(Payment.order_id == order_id).order_by(Payment.created_at.desc()),
    )
    return list(result)
