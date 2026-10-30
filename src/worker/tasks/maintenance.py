"""Регулярная уборка: неоплаченные заказы, заброшенные черновики, старые сообщения; пульс."""

from __future__ import annotations

import logging

from core import health
from core.db import session_scope
from core.services import orders, outbox, settings
from worker.broker import MOSCOW_CRON, broker

logger = logging.getLogger(__name__)


@broker.task(schedule=[{"cron": "*/5 * * * *"}])
async def expire_unpaid_orders() -> None:
    """Отменить заказы, которые не оплатили в отведённое время."""
    async with session_scope() as session:
        expired = await orders.expire_unpaid(session)
        if expired:
            logger.info("Отменено неоплаченных заказов: %s", len(expired))


@broker.task(schedule=[{"cron": "20 4 * * *", **MOSCOW_CRON}])
async def cleanup_drafts() -> None:
    """Удалить черновики, которые клиент бросил и не вернулся, и старые сообщения очереди."""
    async with session_scope() as session:
        rules = await settings.get_order_rules(session)
        removed = await orders.delete_stale_drafts(session, older_than_days=rules.draft_ttl_days)
        if removed:
            logger.info("Удалено заброшенных черновиков: %s", removed)
        purged = await outbox.purge(session)
        if purged:
            logger.info("Удалено старых сообщений из очереди: %s", purged)


@broker.task(schedule=[{"cron": "* * * * *"}])
async def heartbeat() -> None:
    """Пульс для healthcheck воркера. Задачу ставит только планировщик, поэтому свежий
    пульс значит, что живы и планировщик, и воркер."""
    health.beat("worker")
