"""Очередь исходящих сообщений в базе (outbox).

Как сообщение доходит до Telegram:
1. Сервис кладёт запись в `outbox` в той же транзакции, что и изменение заказа.
   Откатилась транзакция — нет и сообщения; закоммитилась — сообщение уже не потеряется.
2. После коммита воркеру ставится задача «разобрать очередь» — чтобы клиент получил
   сообщение сразу. Если Redis в этот момент недоступен, не страшно: тот же разбор
   запускается по расписанию раз в минуту.
3. Обработчик берёт сообщение «в аренду» (переносит next_attempt_at вперёд), отправляет
   и отмечает результат. Упал воркер посреди отправки — аренда истечёт, сообщение
   возьмут снова. Сбой Telegram — повтор с растущей паузой.

Отправленные сообщения хранятся неделю — чтобы было что посмотреть при разборе
жалобы «не пришло», — потом удаляются. Неотправленные хранятся месяц.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.clock import now_utc
from core.db import after_commit
from core.enums import OutboxKind, OutboxStatus
from core.models import OutboxMessage

#: Сколько сообщение «в аренде» у обработчика. Отправка занимает секунды, но если
#: Telegram просит подождать, обработчик ждёт до минуты.
LEASE = dt.timedelta(minutes=5)
#: После стольких неудач сообщение считается недоставленным.
MAX_ATTEMPTS = 12
#: Пауза перед повтором: 30 с, 1 мин, 2 мин… но не больше часа. 12 попыток — около 6 часов.
RETRY_BASE = dt.timedelta(seconds=30)
RETRY_MAX = dt.timedelta(hours=1)

KEEP_SENT = dt.timedelta(days=7)
KEEP_FAILED = dt.timedelta(days=30)


#: Задачу разбора ставим один раз на транзакцию, сколько бы сообщений в ней ни было.
_KICK_SCHEDULED = "outbox_kick_scheduled"


def enqueue(session: AsyncSession, kind: OutboxKind, payload: dict[str, Any]) -> None:
    """Положить сообщение в очередь. Уйдёт после коммита транзакции."""
    session.add(OutboxMessage(kind=kind, payload=payload, status=OutboxStatus.PENDING))
    if not session.info.get(_KICK_SCHEDULED):
        session.info[_KICK_SCHEDULED] = True
        after_commit(session, _kick)


async def _kick() -> None:
    # Импорт внутри: core не должен зависеть от worker на уровне модуля.
    from worker.tasks.outbox import dispatch_outbox

    await dispatch_outbox.kiq()


async def claim_next(
    session: AsyncSession,
    *,
    now: dt.datetime | None = None,
) -> OutboxMessage | None:
    """Взять следующее сообщение, которому подошло время, и продлить его аренду."""
    moment = now or now_utc()
    message = await session.scalar(
        select(OutboxMessage)
        .where(
            OutboxMessage.status == OutboxStatus.PENDING,
            OutboxMessage.next_attempt_at <= moment,
        )
        .order_by(OutboxMessage.id)
        .limit(1)
        .with_for_update(skip_locked=True),
    )
    if message is None:
        return None
    message.attempts += 1
    message.next_attempt_at = moment + LEASE
    await session.flush()
    return message


def retry_delay(attempts: int) -> dt.timedelta:
    """Пауза после `attempts`-й неудачной попытки."""
    return min(RETRY_BASE * (2 ** max(attempts - 1, 0)), RETRY_MAX)


async def mark_sent(session: AsyncSession, message_id: int) -> None:
    message = await session.get(OutboxMessage, message_id)
    if message is not None:
        message.status = OutboxStatus.SENT
        message.sent_at = now_utc()
        message.last_error = None


async def mark_failed(
    session: AsyncSession,
    message_id: int,
    error: str,
    *,
    retry: bool,
) -> OutboxStatus | None:
    """Неудачная попытка: повторить позже или сдаться. Возвращает новое состояние."""
    message = await session.get(OutboxMessage, message_id)
    if message is None:
        return None
    message.last_error = error[:2000]
    if retry and message.attempts < MAX_ATTEMPTS:
        message.next_attempt_at = now_utc() + retry_delay(message.attempts)
    else:
        message.status = OutboxStatus.FAILED
    return message.status


async def purge(session: AsyncSession, *, now: dt.datetime | None = None) -> int:
    """Удалить старые отправленные и недоставленные сообщения."""
    moment = now or now_utc()
    result = await session.execute(
        delete(OutboxMessage).where(
            (
                (OutboxMessage.status == OutboxStatus.SENT)
                & (OutboxMessage.created_at < moment - KEEP_SENT)
            )
            | (
                (OutboxMessage.status == OutboxStatus.FAILED)
                & (OutboxMessage.created_at < moment - KEEP_FAILED)
            ),
        ),
    )
    return result.rowcount or 0
