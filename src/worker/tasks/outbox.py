"""Разбор очереди исходящих сообщений (core.services.outbox).

Запускается двумя путями: сразу после коммита, в котором появилось сообщение, и по
расписанию раз в минуту — это страховка на случай, если постановка задачи не дошла
(Redis недоступен) или воркер перезапускался.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from core.db import session_scope
from core.enums import OutboxKind, OutboxStatus
from core.services import outbox
from worker.broker import broker
from worker.tasks import notify

logger = logging.getLogger(__name__)

#: Сколько сообщений разбираем за один запуск: остальные возьмёт следующий.
BATCH_LIMIT = 200

_HANDLERS: dict[OutboxKind, Callable[[dict[str, Any]], Awaitable[None]]] = {
    OutboxKind.CUSTOMER: lambda payload: notify.notify_customer(
        int(payload["order_id"]),
        str(payload["template"]),
        payload.get("context") or {},
    ),
    OutboxKind.OWNER_ORDER: lambda payload: notify.notify_owner(int(payload["order_id"])),
    OutboxKind.OWNER_TEXT: lambda payload: notify.notify_owner_text(str(payload["text"])),
    OutboxKind.ALERT: lambda payload: notify.notify_alert(str(payload["text"])),
    OutboxKind.STRIP_BUTTONS: lambda payload: notify.strip_order_buttons(int(payload["order_id"])),
}

#: Разбор идёт по одному: так сообщения клиенту уходят в том порядке, в каком появились.
#: Воркер один (см. CLAUDE.md), поэтому блокировки в памяти процесса достаточно.
_lock = asyncio.Lock()


@broker.task(schedule=[{"cron": "* * * * *"}])
async def dispatch_outbox() -> int:
    """Отправить всё, чему подошло время. Возвращает число отправленных сообщений."""
    async with _lock:
        sent = 0
        for _ in range(BATCH_LIMIT):
            async with session_scope() as session:
                message = await outbox.claim_next(session)
                if message is None:
                    break
                message_id, kind, payload = message.id, message.kind, dict(message.payload)

            sent += await _deliver(message_id, kind, payload)
        return sent


async def _deliver(message_id: int, kind: OutboxKind, payload: dict[str, Any]) -> int:
    """Отправить одно сообщение и записать результат. 1 — отправлено."""
    try:
        await _HANDLERS[kind](payload)
    except notify.PermanentError as error:
        logger.warning("Сообщение %s (%s) не доставлено: %s", message_id, kind, error)
        async with session_scope() as session:
            await outbox.mark_failed(session, message_id, str(error), retry=False)
        return 0
    except Exception as error:
        logger.exception("Сбой отправки сообщения %s (%s), будет повтор", message_id, kind)
        async with session_scope() as session:
            status = await outbox.mark_failed(
                session,
                message_id,
                f"{type(error).__name__}: {error}",
                retry=True,
            )
        if status == OutboxStatus.FAILED:
            logger.error(
                "Сообщение %s (%s) так и не отправилось — попытки кончились", message_id, kind
            )
        return 0

    async with session_scope() as session:
        await outbox.mark_sent(session, message_id)
    return 1
