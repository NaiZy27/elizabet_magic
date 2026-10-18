"""Отправления СДЭК: создание, статусы, подписка на вебхук.

Каждое отправление обрабатывается в своей транзакции: если СДЭК не ответил по одному,
остальные не ждут, а следующий запуск попробует снова.
"""

from __future__ import annotations

import asyncio
import logging

from core.config import get_settings
from core.db import session_scope
from core.enums import ACTIVE_SHIPMENT_STATUSES
from core.errors import DomainError, NotFoundError
from core.services import cdek, orders, shipments
from worker.broker import broker

logger = logging.getLogger(__name__)

#: Через сколько секунд спрашивать номер СДЭК после создания заказа: обычно он
#: готов через пару секунд, дальше подхватит сверка по расписанию.
_TRACK_POLL_DELAYS = (2, 3, 5, 10)


@broker.task
async def create_shipment(order_id: int) -> None:
    """Создать отправление СДЭК по собранному заказу и сразу забрать его номер."""
    try:
        async with session_scope() as session:
            order = await orders.get_order(session, order_id)
            shipment = await shipments.create_cdek_shipment(session, order)
            shipment_id = shipment.id
    except NotFoundError:
        return
    except DomainError as error:
        # Повторять вслепую не стоит: ошибка в данных не исчезнет, а сбой связи
        # владелица повторит кнопкой — данные для ручной отправки у неё уже будут.
        logger.warning("Заказ %s: отправление СДЭК не создано: %s", order_id, error.message)
        async with session_scope() as session:
            order = await orders.get_order(session, order_id)
            await shipments.report_create_failure(session, order, error.message)
        return

    for delay in _TRACK_POLL_DELAYS:
        await asyncio.sleep(delay)
        try:
            async with session_scope() as session:
                shipment = await shipments.refresh(session, shipment_id)
                done = (
                    shipment is None
                    or shipment.track_number is not None
                    or shipment.status not in ACTIVE_SHIPMENT_STATUSES
                )
        except DomainError as error:
            logger.warning("Отправление %s: СДЭК не ответил: %s", shipment_id, error.message)
            continue
        if done:
            return


@broker.task
async def refresh_cdek_order(uuid: str) -> None:
    """Вебхук СДЭК: перезапросить заказ у самого СДЭК — уведомлению не верим на слово."""
    async with session_scope() as session:
        shipment_id = await shipments.shipment_id_by_uuid(session, uuid)
    if shipment_id is None:
        logger.info("Вебхук СДЭК про чужой или старый заказ %s — пропускаем", uuid)
        return
    await _refresh(shipment_id)


@broker.task(schedule=[{"cron": "*/30 * * * *"}])
async def sync_cdek_shipments() -> None:
    """Сверить статусы всех живых отправлений — на случай, если вебхук не дошёл."""
    if not get_settings().cdek.is_configured:
        return
    async with session_scope() as session:
        pending = await shipments.cdek_shipments_to_sync(session)
    for shipment_id in pending:
        await _refresh(shipment_id)


@broker.task(schedule=[{"cron": "7 */6 * * *"}])
async def ensure_cdek_webhook() -> None:
    """Подписаться на статусы СДЭК, если подписки нет (например, после смены ключей)."""
    if not get_settings().cdek.is_configured:
        return
    try:
        if await cdek.ensure_webhook():
            logger.info("Подписались на вебхук статусов СДЭК")
    except DomainError as error:
        logger.warning("Не удалось подписаться на вебхук СДЭК: %s", error.message)


async def _refresh(shipment_id: int) -> None:
    try:
        async with session_scope() as session:
            await shipments.refresh(session, shipment_id)
    except DomainError as error:
        logger.warning("Отправление %s: %s", shipment_id, error.message)
