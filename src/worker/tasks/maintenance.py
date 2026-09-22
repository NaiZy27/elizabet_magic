"""Регулярная уборка: неоплаченные заказы и заброшенные черновики."""

from __future__ import annotations

import logging

from core.db import session_scope
from core.services import orders, settings
from worker.broker import broker

logger = logging.getLogger(__name__)


@broker.task(schedule=[{"cron": "*/5 * * * *"}])
async def expire_unpaid_orders() -> None:
    """Отменить заказы, которые не оплатили в отведённое время."""
    async with session_scope() as session:
        expired = await orders.expire_unpaid(session)
        if expired:
            logger.info("Отменено неоплаченных заказов: %s", len(expired))


@broker.task(schedule=[{"cron": "20 4 * * *"}])
async def cleanup_drafts() -> None:
    """Удалить черновики, которые клиент бросил и не вернулся."""
    async with session_scope() as session:
        rules = await settings.get_order_rules(session)
        removed = await orders.delete_stale_drafts(session, older_than_days=rules.draft_ttl_days)
        if removed:
            logger.info("Удалено заброшенных черновиков: %s", removed)
