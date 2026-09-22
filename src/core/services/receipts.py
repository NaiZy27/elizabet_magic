"""Чеки самозанятого.

Как это устроено в НПД: продажа передаётся в «Мой налог», там формируется чек со
ссылкой на печатную форму на lknpd.nalog.ru. При возврате чек не «сторнируется»,
а аннулируется. Чек хранится у нас бессрочно — это подтверждение, что продажа
передана в налоговую.

Кто регистрирует:
- Робочеки СМЗ (Робокасса) — сами, после оплаты; их ответ сюда записывает
  интеграция с Робокассой, когда она будет подключена;
- владелица руками — вставляет ссылку на чек в карточке заказа;
- заглушка оплаты — «регистрирует» тестовый чек без ссылки.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.clock import now_utc
from core.enums import (
    MessageTemplateKey,
    OrderEventType,
    PaymentProvider,
    ReceiptProvider,
    ReceiptStatus,
)
from core.errors import ConflictError, ValidationError
from core.models import Order, Payment, Receipt
from core.services import notifications, orders

#: Адрес, с которого начинаются ссылки на чеки «Мой налог».
NALOG_RECEIPT_HOST = "lknpd.nalog.ru"


def receipt_lines(order: Order) -> list[dict[str, int | str]]:
    """Позиции чека: по строке на бокс с его услугами, отдельно общие услуги и доставка."""
    lines: list[dict[str, int | str]] = []
    for item in order.items:
        lines.append(
            {"name": item.name_snapshot, "amount_kopecks": item.unit_price_kopecks * item.quantity},
        )
        lines.extend(
            {"name": addon.name_snapshot, "amount_kopecks": addon.total_kopecks}
            for addon in item.addons
        )
    lines.extend(
        {"name": addon.name_snapshot, "amount_kopecks": addon.total_kopecks}
        for addon in order.order_addons
    )
    if order.delivery_kopecks:
        lines.append(
            {"name": "Доставка до пункта выдачи", "amount_kopecks": order.delivery_kopecks}
        )
    return lines


async def get_for_payment(session: AsyncSession, payment_id: int) -> Receipt | None:
    return await session.scalar(select(Receipt).where(Receipt.payment_id == payment_id))


async def list_for_order(session: AsyncSession, order_id: int) -> list[Receipt]:
    result = await session.scalars(
        select(Receipt).where(Receipt.order_id == order_id).order_by(Receipt.created_at.desc()),
    )
    return list(result)


async def create_for_payment(session: AsyncSession, payment: Payment, order: Order) -> Receipt:
    """Завести чек по прошедшей оплате. Повторный вызов вернёт тот же чек."""
    existing = await get_for_payment(session, payment.id)
    if existing is not None:
        return existing

    is_stub = payment.provider == PaymentProvider.STUB
    receipt = Receipt(
        order_id=order.id,
        payment_id=payment.id,
        provider=ReceiptProvider.STUB if is_stub else ReceiptProvider.ROBOKASSA,
        status=ReceiptStatus.PENDING,
        amount_kopecks=payment.amount_kopecks,
        buyer_email=order.recipient_email,
        items=receipt_lines(order),
    )
    session.add(receipt)
    await session.flush()

    if is_stub:
        # Тестовая оплата: налоговую не трогаем, но проходим тот же путь, что и боевой чек.
        await register(
            session,
            receipt,
            order,
            external_id=f"TEST-{receipt.id}",
            url=None,
            actor="system",
        )
    return receipt


async def register(
    session: AsyncSession,
    receipt: Receipt,
    order: Order,
    *,
    external_id: str | None,
    url: str | None,
    actor: str,
    provider: ReceiptProvider | None = None,
) -> Receipt:
    """Чек зарегистрирован в «Мой налог». Клиенту уходит ссылка, если она есть."""
    if receipt.status == ReceiptStatus.ANNULLED:
        raise ConflictError("Чек уже аннулирован")
    if url is not None:
        url = url.strip()
        if NALOG_RECEIPT_HOST not in url:
            raise ValidationError(
                f"Ссылка на чек должна вести на {NALOG_RECEIPT_HOST} — "
                "скопируйте её из «Мой налог»",
            )

    receipt.status = ReceiptStatus.REGISTERED
    receipt.external_id = (external_id or "").strip() or receipt.external_id
    receipt.url = url or receipt.url
    receipt.registered_at = receipt.registered_at or now_utc()
    if provider is not None:
        receipt.provider = provider
    await session.flush()

    await orders.log_event(
        session,
        order,
        OrderEventType.RECEIPT_REGISTERED,
        payload={"receipt_id": receipt.id, "external_id": receipt.external_id},
        actor=actor,
    )
    if receipt.url:
        notifications.schedule_customer_notification(
            session,
            order,
            MessageTemplateKey.RECEIPT_ISSUED,
            receipt_url=receipt.url,
        )
    return receipt


async def register_manual(
    session: AsyncSession,
    order: Order,
    *,
    url: str,
    external_id: str | None,
    actor: str,
) -> Receipt:
    """Владелица пробила чек в «Мой налог» сама и вставила ссылку в панели."""
    payment = await session.scalar(
        select(Payment)
        .where(Payment.order_id == order.id, Payment.paid_at.is_not(None))
        .order_by(Payment.paid_at.desc())
        .limit(1),
    )
    if payment is None:
        raise ConflictError("Чек привязывается к оплате, а заказ ещё не оплачен")
    if not url.strip():
        raise ValidationError("Вставьте ссылку на чек")

    receipt = await get_for_payment(session, payment.id)
    if receipt is None:
        receipt = Receipt(
            order_id=order.id,
            payment_id=payment.id,
            provider=ReceiptProvider.MANUAL,
            status=ReceiptStatus.PENDING,
            amount_kopecks=payment.amount_kopecks,
            buyer_email=order.recipient_email,
            items=receipt_lines(order),
        )
        session.add(receipt)
        await session.flush()

    return await register(
        session,
        receipt,
        order,
        external_id=external_id,
        url=url,
        actor=actor,
        provider=ReceiptProvider.MANUAL,
    )


async def annul(
    session: AsyncSession,
    receipt: Receipt,
    order: Order,
    *,
    reason: str,
    actor: str,
) -> Receipt:
    """Аннулировать чек — так в НПД оформляется возврат."""
    if receipt.status == ReceiptStatus.ANNULLED:
        return receipt
    receipt.status = ReceiptStatus.ANNULLED
    receipt.annulled_at = now_utc()
    receipt.annul_reason = reason
    await session.flush()
    await orders.log_event(
        session,
        order,
        OrderEventType.RECEIPT_ANNULLED,
        payload={"receipt_id": receipt.id, "reason": reason},
        actor=actor,
    )
    return receipt
