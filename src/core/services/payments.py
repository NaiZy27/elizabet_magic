"""Оплата заказа.

Пока подключён только внутренний провайдер-заглушка: оплата подтверждается со
страницы /pay вручную. Робокасса встанет на это же место — создание платежа,
идемпотентное подтверждение и переход заказа в очередь останутся без изменений.

Подтверждение оплаты идемпотентно: уведомление о платеже может прийти несколько раз,
но заказ должен быть оплачен ровно один раз.

Оплата, пришедшая после отмены, не теряется: деньги у нас, значит заказ либо
возвращается в работу (если его отменили мы по сроку оплаты), либо владелица
получает сигнал, что нужен возврат.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.clock import now_utc
from core.config import get_settings
from core.enums import (
    CancelReason,
    MessageTemplateKey,
    OrderEventType,
    OrderStatus,
    PaymentProvider,
    PaymentStatus,
    ProviderEnvironment,
    ReceiptStatus,
)
from core.errors import ConflictError, NotFoundError, ValidationError
from core.models import Order, Payment
from core.money import format_rubles
from core.services import notifications, receipts
from core.services import orders as orders_service

logger = logging.getLogger(__name__)


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


#: Адреса, которые видны только на этой машине: Telegram такие ссылки в кнопке не примет.
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


def is_button_url(url: str) -> bool:
    """Можно ли повесить ссылку на кнопку в Telegram.

    На машине разработчика PUBLIC_BASE_URL обычно http://localhost:8000 — такую
    кнопку Telegram отклоняет, и вместо неё бот показывает ссылку текстом.
    """
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return False
    return parts.hostname not in LOCAL_HOSTS and not parts.hostname.endswith(".local")


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
    Оплата по отменённому заказу не отклоняется — см. `_accept_late_payment`.
    """
    # Блокируем строку: два одновременных уведомления не должны оплатить заказ дважды.
    payment = await session.scalar(
        select(Payment).where(Payment.id == payment_id).with_for_update(),
    )
    if payment is None:
        raise NotFoundError("Платёж не найден")

    if payment.status in {PaymentStatus.PAID, PaymentStatus.REFUNDED}:
        return payment
    if amount_kopecks is not None and amount_kopecks != payment.amount_kopecks:
        raise ValidationError("Оплаченная сумма не совпадает с суммой заказа")

    order = await orders_service.get_order(session, payment.order_id)
    if payment.status != PaymentStatus.PENDING:
        return await _accept_late_payment(
            session,
            payment,
            order,
            external_id=external_id,
            raw_result=raw_result,
            fee_kopecks=fee_kopecks,
            actor=actor,
        )

    _fill_paid(payment, external_id=external_id, raw_result=raw_result, fee_kopecks=fee_kopecks)
    await session.flush()
    await orders_service.mark_paid(session, order, actor=actor)
    await receipts.create_for_payment(session, payment, order)
    return payment


async def _accept_late_payment(
    session: AsyncSession,
    payment: Payment,
    order: Order,
    *,
    external_id: str | None,
    raw_result: dict[str, Any] | None,
    fee_kopecks: int | None,
    actor: str,
) -> Payment:
    """Деньги пришли по закрытой попытке оплаты.

    Так бывает, когда клиент открыл страницу оплаты, отвлёкся, заказ отменился
    по сроку, а потом клиент всё-таки заплатил. Платёжной системе отвечаем «принято»
    в любом случае — иначе она будет присылать уведомление снова и снова.
    """
    if orders_service.can_revive(order):
        await orders_service.revive_expired(session, order, actor=actor)
        payment.status = PaymentStatus.PAID
        _fill_paid(payment, external_id=external_id, raw_result=raw_result, fee_kopecks=fee_kopecks)
        await session.flush()
        await orders_service.mark_paid(session, order, actor=actor)
        await receipts.create_for_payment(session, payment, order)
        notifications.schedule_owner_text(
            session,
            f"Оплата по заказу {order.admin_label} пришла после автоотмены — "
            "заказ вернулся в очередь.",
        )
        return payment

    if order.paid_at is None:
        # Заказ отменили клиент или владелица: вернуть его нельзя, но оплата была —
        # фиксируем её и чек, владелице нужен возврат.
        payment.status = PaymentStatus.PAID
        problem = "заказ был отменён до оплаты"
    else:
        # Заказ уже оплачен другой попыткой: вторую оплату держим отдельно.
        problem = "заказ уже был оплачен — это повторная оплата"
    _fill_paid(payment, external_id=external_id, raw_result=raw_result, fee_kopecks=fee_kopecks)
    await session.flush()
    if payment.status == PaymentStatus.PAID:
        await receipts.create_for_payment(session, payment, order)

    await orders_service.log_event(
        session,
        order,
        OrderEventType.PAYMENT_AFTER_CANCEL,
        payload={"payment_id": payment.id, "problem": problem},
        actor=actor,
    )
    logger.warning("Оплата %s по заказу %s: %s", payment.id, order.id, problem)
    notifications.schedule_owner_text(
        session,
        f"⚠️ Пришла оплата {format_rubles(payment.amount_kopecks)} по заказу "
        f"{order.admin_label}, но {problem}. Нужно вернуть деньги клиенту.",
    )
    return payment


def _fill_paid(
    payment: Payment,
    *,
    external_id: str | None,
    raw_result: dict[str, Any] | None,
    fee_kopecks: int | None,
) -> None:
    if payment.status == PaymentStatus.PENDING:
        payment.status = PaymentStatus.PAID
    payment.paid_at = payment.paid_at or now_utc()
    payment.external_id = external_id
    payment.raw_result = raw_result
    payment.fee_kopecks = fee_kopecks


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


async def refund_order(
    session: AsyncSession,
    order: Order,
    *,
    actor: str,
    reason: str,
) -> Payment:
    """Вернуть деньги за заказ целиком.

    ЗАТЫЧКА первого этапа: деньги владелица возвращает сама (переводом или в кабинете
    Робокассы), а здесь фиксируется факт возврата, аннулируется чек и отменяется заказ.
    Когда подключим Робокассу, на месте `_refund_with_provider` будет вызов её API
    возврата — он же аннулирует чек в «Мой налог».
    """
    payment = await session.scalar(
        select(Payment)
        .where(Payment.order_id == order.id, Payment.status == PaymentStatus.PAID)
        .with_for_update(),
    )
    if payment is None:
        raise ConflictError("По заказу нет оплаты, которую можно вернуть")

    await _refund_with_provider(payment)

    payment.status = PaymentStatus.REFUNDED
    payment.refunded_at = now_utc()
    order.refunded_at = payment.refunded_at
    await session.flush()

    receipt = await receipts.get_for_payment(session, payment.id)
    if receipt is not None and receipt.status != ReceiptStatus.ANNULLED:
        await receipts.annul(session, receipt, order, reason=f"возврат: {reason}", actor=actor)

    await orders_service.log_event(
        session,
        order,
        OrderEventType.REFUNDED,
        payload={
            "payment_id": payment.id,
            "amount_kopecks": payment.amount_kopecks,
            "reason": reason,
        },
        actor=actor,
    )

    if order.status == OrderStatus.CANCELLED:
        notifications.schedule_customer_notification(
            session,
            order,
            MessageTemplateKey.ORDER_REFUNDED,
        )
    else:
        await orders_service.cancel(
            session,
            order,
            reason=CancelReason.REFUND,
            actor=actor,
            comment=reason,
            template_key=MessageTemplateKey.ORDER_REFUNDED,
        )
    return payment


async def _refund_with_provider(payment: Payment) -> None:
    """Возврат через платёжную систему.

    TODO: вызвать API возврата Робокассы, когда магазин будет подключён. Сейчас
    деньги возвращаются вручную, поэтому здесь только проверка, что это не ошибка.
    """
    if payment.provider == PaymentProvider.ROBOKASSA:
        logger.warning(
            "Возврат по платежу %s отмечен вручную: API возврата Робокассы ещё не подключён",
            payment.id,
        )


async def list_payments(session: AsyncSession, order_id: int) -> list[Payment]:
    result = await session.scalars(
        select(Payment).where(Payment.order_id == order_id).order_by(Payment.created_at.desc()),
    )
    return list(result)
