"""Обновление пунктов выдачи из публичных источников (core.services.pickup_sync).

Пункты обновляются раз в неделю, но задача запускается каждую ночь и берёт только
службы, у которых последнее успешное обновление старше `REFRESH_EVERY`. Так оборванный
запуск (деплой или перезапуск воркера посреди многочасовой загрузки Ozon, источник
не ответил) повторится сам следующей ночью, а не через неделю.

Обновляем и выключенные в панели службы: тогда при включении пункты уже свежие.
Сбой одной службы не мешает другой; ошибка остаётся в панели, в разделе «Доставка»,
и приходит тревогой в Telegram.
"""

from __future__ import annotations

import datetime as dt
import logging

from core.db import session_scope
from core.errors import DomainError
from core.labels import delivery_provider_label
from core.services import notifications, pickup_sync
from worker.broker import MOSCOW_CRON, broker

logger = logging.getLogger(__name__)

#: Чуть меньше недели: ночной запуск не должен промахнуться на несколько минут.
REFRESH_EVERY = dt.timedelta(days=6, hours=12)


@broker.task(schedule=[{"cron": "40 0 * * *", **MOSCOW_CRON}])
async def refresh_manual_pickup_points() -> None:
    """Ночью: пункты служб из их публичных карт — тех служб, чья очередь подошла."""
    async with session_scope() as session:
        due = await pickup_sync.providers_due(session, older_than=REFRESH_EVERY)

    for provider in due:
        try:
            stats = await pickup_sync.sync_provider(provider)
        except Exception as error:
            message = error.message if isinstance(error, DomainError) else repr(error)
            logger.exception("Пункты %s не обновлены", provider)
            async with session_scope() as session:
                notifications.schedule_alert(
                    session,
                    f"Пункты выдачи {delivery_provider_label(provider)} не обновились: "
                    f"{message}. Следующая попытка — завтра ночью.",
                )
            continue
        logger.info("Пункты %s обновлены: %s", provider, dict(stats))
