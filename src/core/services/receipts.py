"""Чеки самозанятого.

Как это устроено в НПД: продажа передаётся в «Мой налог», там формируется чек со
ссылкой на печатную форму на lknpd.nalog.ru. Чек хранится у нас бессрочно — это
подтверждение, что продажа передана в налоговую. Возвратов в системе нет: если
деньги вернули, чек владелица аннулирует сама в «Мой налог».

Кто регистрирует:
- Робочеки СМЗ (Робокасса) — сами, после оплаты: состав чека уходит в кассу вместе
  со ссылкой на оплату, Робочеки передают продажу в «Мой налог» и отправляют чек
  клиенту на email. Ссылку на чек Робокасса по API не отдаёт, поэтому у нас такой
  чек — «передан в Робочеки» (SENT); ссылку владелица может вставить из кабинета;
- владелица руками — если состав чека в кассу не ушёл (сумма строк не сошлась
  с оплатой) или оплата была без Робочеков; вставляет ссылку в карточке заказа;
- заглушка оплаты и тестовый режим Робокассы — «регистрируют» тестовый чек без ссылки.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.clock import now_utc
from core.enums import (
    MessageTemplateKey,
    OrderEventType,
    PaymentProvider,
    ProviderEnvironment,
    ReceiptProvider,
    ReceiptStatus,
)
from core.errors import ConflictError, ValidationError
from core.models import Order, Payment, Receipt
from core.services import notifications, orders, robokassa

#: Адрес, с которого начинаются ссылки на чеки «Мой налог».
NALOG_RECEIPT_HOST = "lknpd.nalog.ru"


def receipt_lines(order: Order) -> list[dict[str, int | str]]:
    """Позиции чека: по строке на бокс с его услугами, отдельно общие услуги и доставка.

    Суммы — те же, что ушли в Робочеки: в режиме проверки по 1 ₽ за строку.
    """
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
    for line in lines:
        line["amount_kopecks"] = robokassa.line_kopecks(int(line["amount_kopecks"]))
    return [line for line in lines if line["amount_kopecks"]]


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

    # Заглушка и тестовый режим Робокассы: денег нет, Робочеки такую оплату не видят.
    is_stub = (
        payment.provider == PaymentProvider.STUB or payment.environment == ProviderEnvironment.TEST
    )
    by_robochecks = not is_stub and payment.receipt_sent
    if is_stub:
        provider = ReceiptProvider.STUB
    else:
        provider = ReceiptProvider.ROBOKASSA if by_robochecks else ReceiptProvider.MANUAL
    receipt = Receipt(
        order_id=order.id,
        payment_id=payment.id,
        provider=provider,
        status=ReceiptStatus.SENT if by_robochecks else ReceiptStatus.PENDING,
        amount_kopecks=payment.amount_kopecks,
        buyer_email=order.recipient_email,
        items=receipt_lines(order),
    )
    if by_robochecks:
        receipt.registered_at = now_utc()
    session.add(receipt)
    await session.flush()

    if not is_stub and not by_robochecks:
        notifications.schedule_owner_text(
            session,
            f"🧾 По заказу {order.admin_label} чек не ушёл в Робочеки — пробейте его "
            "в «Мой налог» вручную и вставьте ссылку в карточке заказа.",
        )
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

    # Ссылку на чек Робочеков владелица вставляет из их кабинета — чек от этого
    # не становится «ручным».
    provider = None if receipt.provider == ReceiptProvider.ROBOKASSA else ReceiptProvider.MANUAL
    return await register(
        session,
        receipt,
        order,
        external_id=external_id,
        url=url,
        actor=actor,
        provider=provider,
    )
