"""Еженедельное обновление пунктов выдачи из публичных источников (core.services.pickup_sync).

Обновляем и выключенные в панели службы: тогда при включении пункты уже свежие.
Сбой одной службы не мешает другой; ошибка остаётся в панели, в разделе «Доставка».
"""

from __future__ import annotations

import logging

from core.errors import DomainError
from core.services import pickup_sync
from worker.broker import broker

logger = logging.getLogger(__name__)


@broker.task(schedule=[{"cron": "40 0 * * 1"}])
async def refresh_manual_pickup_points() -> None:
    """Понедельник, ночь: пункты служб из их публичных карт."""
    for provider in pickup_sync.SOURCES:
        try:
            stats = await pickup_sync.sync_provider(provider)
        except DomainError as error:
            logger.error("Пункты %s не обновлены: %s", provider, error.message)
            continue
        logger.info("Пункты %s обновлены: %s", provider, dict(stats))
