"""Оплата заказа.

Деньги принимает Робокасса (PAYMENTS_MODE=robokassa) или, на тестовом контуре,
заглушка: оплата подтверждается кнопкой на странице /pay.

Как оплата попадает к нам:
- уведомление Робокассы на ResultURL — основной путь;
- сверка (`reconcile`) — фоновая задача спрашивает Робокассу о счетах, по которым
  клиент уходил в кассу, но уведомления не было. Так оплата не теряется, если
  ResultURL не дошёл: сервер перезагружался, в кабинете опечатка в адресе.

Подтверждение оплаты идемпотентно: уведомление о платеже может прийти несколько раз,
но заказ должен быть оплачен ровно один раз.

Оплата, пришедшая после отмены, не теряется: деньги у нас, значит заказ либо
возвращается в работу (если его отменили мы по сроку оплаты), либо владелица
получает сигнал вернуть деньги.

Возвратов в системе нет: клиент пишет в поддержку, владелица возвращает деньги
в кабинете Робокассы и аннулирует чек в «Мой налог», а заказ отменяет в панели.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any
from urllib.parse import urlsplit

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
from core.money import format_rubles
from core.services import notifications, receipts, robokassa
from core.services import orders as orders_service

logger = logging.getLogger(__name__)


def current_provider() -> PaymentProvider:
    """Кто принимает деньги — решает PAYMENTS_MODE, а не наличие ключей.

    Так заглушка не включается сама по себе из-за незаполненного .env: чтобы
    принимать «оплату» кнопкой, режим нужно выставить руками.
    """
    return PaymentProvider.ROBOKASSA if get_settings().payments_are_real else PaymentProvider.STUB


def current_environment() -> ProviderEnvironment:
    settings = get_settings()
    return (
        ProviderEnvironment.TEST if settings.robokassa.is_test else ProviderEnvironment.PRODUCTION
    )


def payment_url(payment: Payment) -> str:
    """Ссылка, которую бот даёт клиенту. Id платежа в адресе не светится.

    С Робокассой ссылка ведёт на /pay/<токен>/go: сервер проверяет, что счёт ещё
    действует, и сразу перенаправляет клиента в Робокассу — без промежуточной
    страницы. Подписанную ссылку Робокассы в кнопку не кладём: она устарела бы
    после отмены или изменения заказа. Заглушке нужна страница с кнопкой
    «оплачено», поэтому она остаётся на /pay/<токен>.
    """
    base = get_settings().public_base_url
    if payment.provider == PaymentProvider.ROBOKASSA:
        return f"{base}/pay/{payment.public_token}/go"
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
    amount = payable_kopecks(order)
    if order.total_kopecks <= 0:
        raise ValidationError("Сумма заказа должна быть больше нуля")

    existing = await get_pending_payment(session, order.id)
    if existing is not None:
        if existing.amount_kopecks == amount and is_current(existing):
            return existing
        # Заказ изменился после создания ссылки или сменился способ оплаты (заглушку
        # заменили Робокассой, тестовый режим — боевым) — старая попытка не годится.
        existing.status = PaymentStatus.CANCELLED
        await session.flush()

    payment = Payment(
        order_id=order.id,
        provider=current_provider(),
        environment=current_environment(),
        public_token=uuid.uuid4(),
        amount_kopecks=amount,
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


def payable_kopecks(order: Order) -> int:
    """Сколько списать за заказ. Обычно — его итог; в режиме проверки — по 1 ₽ за строку чека."""
    if get_settings().payments_test_amount_kopecks:
        return sum(int(line["amount_kopecks"]) for line in receipts.receipt_lines(order))
    return order.total_kopecks


def is_current(payment: Payment) -> bool:
    """Выставлена ли попытка тем же способом, каким деньги принимаются сейчас.

    Попытку из тестового режима Робокассы нельзя оплатить в боевом: номер счёта уже
    мог использоваться для тестовой операции, а сверка тестовые счета не видит.
    """
    if payment.provider != current_provider():
        return False
    return payment.provider == PaymentProvider.STUB or payment.environment == current_environment()


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
    order_id = await session.scalar(select(Payment.order_id).where(Payment.id == payment_id))
    if order_id is None:
        raise NotFoundError("Платёж не найден")

    # Блокируем сначала заказ, потом платёж — в том же порядке, что и отмена заказа.
    # Так оплата не разминётся с автоотменой по сроку, а два одновременных уведомления
    # не оплатят заказ дважды.
    order = await orders_service.get_order(session, order_id)
    await orders_service.lock(session, order)
    payment = await session.scalar(
        select(Payment).where(Payment.id == payment_id).with_for_update(),
    )
    await session.refresh(payment)

    if payment.status == PaymentStatus.PAID:
        return payment
    if amount_kopecks is not None and amount_kopecks != payment.amount_kopecks:
        raise ValidationError("Оплаченная сумма не совпадает с суммой заказа")

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
        f"{order.admin_label}, но {problem}. Верните деньги клиенту в кабинете "
        "Робокассы и аннулируйте чек в «Мой налог».",
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


# --- сверка с Робокассой ---

#: Сколько ждём уведомления ResultURL, прежде чем спрашивать Робокассу самим.
RECONCILE_AFTER = dt.timedelta(minutes=5)
#: Как долго после перехода в кассу продолжаем проверять счёт.
RECONCILE_WINDOW = dt.timedelta(days=3)


def reconcile_enabled() -> bool:
    """OpStateExt не видит тестовых оплат — сверка имеет смысл только в боевом режиме."""
    settings = get_settings()
    return settings.payments_are_real and not settings.robokassa.is_test


async def payments_to_reconcile(
    session: AsyncSession,
    *,
    now: dt.datetime | None = None,
    limit: int = 100,
) -> list[int]:
    """Неоплаченные у нас счета, по которым клиент уходил в Робокассу.

    Берём и закрытые попытки: клиент мог заплатить на уже открытой странице кассы
    после автоотмены заказа — такую оплату `confirm_payment` тоже примет.
    """
    moment = now or now_utc()
    result = await session.scalars(
        select(Payment.id)
        .where(
            Payment.provider == PaymentProvider.ROBOKASSA,
            Payment.environment == ProviderEnvironment.PRODUCTION,
            Payment.paid_at.is_(None),
            Payment.status.in_(
                [PaymentStatus.PENDING, PaymentStatus.CANCELLED, PaymentStatus.FAILED],
            ),
            Payment.checkout_started_at.is_not(None),
            Payment.checkout_started_at < moment - RECONCILE_AFTER,
            Payment.checkout_started_at > moment - RECONCILE_WINDOW,
        )
        .order_by(Payment.checkout_started_at.desc())
        .limit(limit),
    )
    return list(result)


async def reconcile(session: AsyncSession, payment_id: int) -> bool:
    """Спросить Робокассу о счёте и провести оплату, если она там есть.

    True — оплата нашлась и проведена этим вызовом.
    """
    state = await robokassa.operation_state(payment_id)
    if state is None or not state.is_paid:
        return False

    paid_before = await session.scalar(select(Payment.paid_at).where(Payment.id == payment_id))
    if paid_before is not None:
        # Пока мы спрашивали, пришло уведомление — оплата уже проведена.
        return False
    payment = await confirm_payment(
        session,
        payment_id,
        external_id=str(payment_id),
        raw_result={
            "source": "OpStateExt",
            "state": state.code,
            "inc_sum": state.inc_sum,
            "payment_method": state.payment_method,
        },
    )
    if (payment.raw_result or {}).get("source") != "OpStateExt":
        # Уведомление успело провести оплату, пока мы ждали блокировку.
        return False
    order = await orders_service.get_order(session, payment.order_id)
    logger.warning("Оплата по счёту %s найдена сверкой: ResultURL не дошёл", payment_id)
    notifications.schedule_owner_text(
        session,
        f"Оплата по заказу {order.admin_label} подтверждена сверкой с Робокассой: "
        "её уведомление до нас не дошло. Если такое повторяется, проверьте Result URL "
        "в технических настройках магазина.",
    )
    return True


def mark_checkout_started(payment: Payment, order: Order) -> dict[str, str]:
    """Клиент уходит в Робокассу: запомнить это и собрать поля формы оплаты.

    Здесь же фиксируется, передан ли в кассу состав чека — от этого зависит,
    пробьют ли чек Робочеки или его нужно оформить руками.
    """
    payment.checkout_started_at = payment.checkout_started_at or now_utc()
    payment.receipt_sent = robokassa.receipt_json(order, payment.amount_kopecks) is not None
    return robokassa.checkout_fields(payment, order)


async def list_payments(session: AsyncSession, order_id: int) -> list[Payment]:
    result = await session.scalars(
        select(Payment).where(Payment.order_id == order_id).order_by(Payment.created_at.desc()),
    )
    return list(result)
