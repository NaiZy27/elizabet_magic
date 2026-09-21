"""Задачи отправки сообщений: клиенту по статусам заказа и владелице о новом заказе."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from core.clock import format_date, now_utc
from core.config import get_settings
from core.db import session_scope
from core.enums import MessageTemplateKey, OrderEventType
from core.errors import NotFoundError
from core.money import format_rubles
from core.services import notifications
from core.services import orders
from worker.broker import broker
from worker.telegram import get_bot

logger = logging.getLogger(__name__)

#: Сколько ждём, если Telegram просит притормозить.
_MAX_RETRY_WAIT_SECONDS = 60


@broker.task
async def notify_customer(
    order_id: int,
    template_key: str,
    extra: dict[str, Any] | None = None,
) -> None:
    """Отправить клиенту сообщение по шаблону.

    Задача ставится после коммита, поэтому заказ уже точно сохранён.
    """
    try:
        key = MessageTemplateKey(template_key)
    except ValueError:
        logger.error("Неизвестный шаблон уведомления: %s", template_key)
        return

    async with session_scope() as session:
        try:
            order = await orders.get_order(session, order_id)
        except NotFoundError:
            logger.warning("Заказ %s исчез до отправки уведомления", order_id)
            return

        text = await notifications.render_for_order(session, order, key, **(extra or {}))
        if not text:
            # Шаблон выключен в админке — это нормальная ситуация, не ошибка.
            return

        customer = order.customer
        try:
            await _send(customer.telegram_user_id, text)
        except TelegramForbiddenError:
            # Клиент заблокировал бота: помечаем и больше не пытаемся.
            customer.bot_blocked_at = now_utc()
            await orders.log_event(
                session,
                order,
                OrderEventType.NOTIFICATION_FAILED,
                payload={"template": key.value, "reason": "бот заблокирован клиентом"},
            )
            return
        except Exception as error:  # noqa: BLE001 — причину пишем в историю заказа
            logger.exception("Не удалось отправить уведомление по заказу %s", order_id)
            await orders.log_event(
                session,
                order,
                OrderEventType.NOTIFICATION_FAILED,
                payload={"template": key.value, "reason": type(error).__name__},
            )
            raise

        await orders.log_event(
            session,
            order,
            OrderEventType.NOTIFICATION_SENT,
            payload={"template": key.value},
        )


@broker.task
async def notify_owner(order_id: int) -> None:
    """Короткая карточка нового оплаченного заказа — владелице."""
    settings = get_settings()
    chat_id = settings.owner.owner_chat_id
    if chat_id is None:
        logger.warning("OWNER_CHAT_ID не задан — владелице не о чем сообщать")
        return

    async with session_scope() as session:
        try:
            order = await orders.get_order(session, order_id)
        except NotFoundError:
            return

        snapshot = order.pickup_snapshot or {}
        lines = [
            f"<b>Новый заказ {order.display_number}</b>",
            "",
            notifications.describe_items(order),
            "",
            f"Сумма: {format_rubles(order.total_kopecks)}",
            f"Город: {snapshot.get('city', '—')}",
        ]
        if notifications.has_video(order):
            lines.append("🎥 Нужно снять видео сборки")
        if order.customer_comment:
            lines.append("✍️ Есть пожелания клиента")
        if order.promised_ready_date:
            lines.append(f"Обещали к {format_date(order.promised_ready_date)}")
        lines.append("")
        lines.append(f"{settings.admin_base_url}/admin/orders/{order.id}")

        try:
            await _send(chat_id, "\n".join(lines))
        except TelegramForbiddenError:
            logger.error("Владелица не нажала /start в боте — уведомления ей не дойдут")


@broker.task
async def notify_owner_text(text: str) -> None:
    """Служебное сообщение владелице (ошибка синхронизации, проблема с доставкой)."""
    chat_id = get_settings().owner.owner_chat_id
    if chat_id is None:
        return
    await _send(chat_id, text)


async def _send(chat_id: int, text: str) -> None:
    """Отправить сообщение, один раз переждав ограничение частоты."""
    bot = get_bot()
    try:
        await bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=True)
    except TelegramRetryAfter as error:
        wait = min(error.retry_after, _MAX_RETRY_WAIT_SECONDS)
        logger.info("Telegram просит подождать %s с", wait)
        await asyncio.sleep(wait)
        await bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=True)
