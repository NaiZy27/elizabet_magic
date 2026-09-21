"""Брокер Taskiq поверх Redis.

Очередь задач живёт в отдельной базе Redis: чистка кэша тарифов не должна
выносить неотправленные уведомления.
"""

from __future__ import annotations

from taskiq import TaskiqScheduler
from taskiq.schedule_sources import LabelScheduleSource
from taskiq_redis import ListQueueBroker

from core.config import get_settings

_settings = get_settings()

broker = ListQueueBroker(url=_settings.redis_broker_url)

#: Расписание собирается из меток `schedule=[...]` у самих задач.
scheduler = TaskiqScheduler(broker=broker, sources=[LabelScheduleSource(broker)])


async def ensure_started() -> None:
    """Подготовить брокер в процессах, которые только ставят задачи (bot, web)."""
    if not broker.is_worker_process:
        await broker.startup()


async def shutdown() -> None:
    await broker.shutdown()
