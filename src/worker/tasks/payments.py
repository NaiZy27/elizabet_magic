"""Сверка с Робокассой: оплаты, уведомление о которых до нас не дошло.

Каждый счёт обрабатывается в своей транзакции: сбой на одном (Робокасса не ответила,
заказ поменяли параллельно) не мешает остальным, а следующий запуск попробует снова.
"""

from __future__ import annotations

import logging

from core.db import session_scope
from core.errors import DomainError
from core.services import payments
from worker.broker import broker

logger = logging.getLogger(__name__)


@broker.task(schedule=[{"cron": "*/10 * * * *"}])
async def reconcile_payments() -> None:
    """Найти оплаты, о которых Робокасса не смогла нам сообщить."""
    if not payments.reconcile_enabled():
        return
    async with session_scope() as session:
        candidates = await payments.payments_to_reconcile(session)

    found = 0
    for payment_id in candidates:
        try:
            async with session_scope() as session:
                found += await payments.reconcile(session, payment_id)
        except DomainError as error:
            logger.warning("Сверка счёта %s не удалась: %s", payment_id, error.message)
    if found:
        logger.info("Сверка нашла оплат без уведомления: %s", found)
