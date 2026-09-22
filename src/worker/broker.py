"""Брокер Taskiq поверх Redis.

Очередь задач живёт в отдельной базе Redis: чистка кэша тарифов не должна
выносить неотправленные уведомления.
"""

from __future__ import annotations

from taskiq import TaskiqScheduler
from taskiq.middlewares import SmartRetryMiddleware
from taskiq.schedule_sources import LabelScheduleSource
from taskiq_redis import ListQueueBroker

from core.config import get_settings

_settings = get_settings()

#: Повторы включаются у задачи меткой retry_on_error=True. Пауза растёт: 10, 20, 40 с…
_retries = SmartRetryMiddleware(
    default_retry_count=5,
    default_delay=10,
    use_delay_exponent=True,
    max_delay_exponent=300,
    use_jitter=True,
)

broker = ListQueueBroker(url=_settings.redis_broker_url).with_middlewares(_retries)

#: Расписание собирается из меток `schedule=[...]` у самих задач.
scheduler = TaskiqScheduler(broker=broker, sources=[LabelScheduleSource(broker)])


async def ensure_started() -> None:
    """Подготовить брокер в процессах, которые только ставят задачи (bot, web)."""
    if not broker.is_worker_process:
        await broker.startup()


async def shutdown() -> None:
    await broker.shutdown()
