"""Задачи отправки сообщений: клиенту по статусам заказа, владелице, снятие старых кнопок.

Отправка идёт вне транзакции: сначала читаем и собираем текст, потом отправляем,
потом отдельной короткой транзакцией пишем результат в историю заказа. Так запись
о неудаче не откатывается вместе с ошибкой, а повтор задачи (retry_on_error) не
зависит от того, успела ли база что-то записать.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)

from core.clock import format_date, now_utc
from core.config import get_settings
from core.db import session_scope
from core.enums import MessageTemplateKey, OrderEventType, OrderStatus
from core.errors import NotFoundError
from core.models import Customer, Order
from core.money import format_rubles
from core.services import notifications, orders
from worker.broker import broker
from worker.telegram import get_bot

logger = logging.getLogger(__name__)

#: Сколько ждём, если Telegram просит притормозить.
_MAX_RETRY_WAIT_SECONDS = 60


@broker.task(retry_on_error=True)
async def notify_customer(
    order_id: int,
    template_key: str,
    extra: dict[str, Any] | None = None,
) -> None:
    """Отправить клиенту сообщение по шаблону.

    Задача ставится после коммита, поэтому заказ уже точно сохранён. Сетевые сбои
    и ответы 5xx повторяются брокером; «бот заблокирован» и «чат не найден» — нет:
    повтор тут не поможет.
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
        chat_id = order.customer.telegram_user_id
        customer_id = order.customer_id

    if not text:
        # Шаблон выключен в админке — это нормальная ситуация, не ошибка.
        return

    try:
        await _send(chat_id, text)
    except TelegramForbiddenError:
        await _record(order_id, key, sent=False, reason="бот заблокирован клиентом")
        async with session_scope() as session:
            customer = await session.get(Customer, customer_id)
            if customer is not None:
                customer.bot_blocked_at = now_utc()
        return
    except TelegramBadRequest as error:
        logger.error("Telegram отклонил уведомление по заказу %s: %s", order_id, error)
        await _record(order_id, key, sent=False, reason=f"Telegram: {error.message}")
        return
    except Exception as error:
        logger.exception("Не удалось отправить уведомление по заказу %s", order_id)
        await _record(order_id, key, sent=False, reason=f"{type(error).__name__}, будет повтор")
        raise

    await _record(order_id, key, sent=True)


async def _record(
    order_id: int,
    key: MessageTemplateKey,
    *,
    sent: bool,
    reason: str | None = None,
) -> None:
    """Записать результат отправки в историю. Сбой записи не должен вызвать повтор отправки."""
    payload: dict[str, Any] = {"template": key.value}
    if reason:
        payload["reason"] = reason
    try:
        async with session_scope() as session:
            order = await orders.get_order(session, order_id)
            await orders.log_event(
                session,
                order,
                OrderEventType.NOTIFICATION_SENT if sent else OrderEventType.NOTIFICATION_FAILED,
                payload=payload,
            )
    except Exception:
        logger.exception("Не удалось записать результат уведомления по заказу %s", order_id)


@broker.task(retry_on_error=True)
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
            f"<b>Новый заказ {order.admin_label}</b>",
            "",
            notifications.describe_items(order),
            "",
            f"Сумма: {format_rubles(order.total_kopecks)}",
            f"Город: {snapshot.get('city', '—')}",
        ]
        if order.video_box_count:
            lines.append(f"🎥 Снять видео: {order.video_box_count} из {order.box_count}")
        if order.has_wishes:
            lines.append("✍️ Есть пожелания клиента")
        if order.promised_ready_date:
            lines.append(f"Обещали к {format_date(order.promised_ready_date)}")
        lines.append("")
        lines.append(f"{settings.admin_base_url}/admin/orders/{order.id}")
        text = "\n".join(lines)

    try:
        await _send(chat_id, text)
    except TelegramForbiddenError:
        logger.error("Владелица не нажала /start в боте — уведомления ей не дойдут")


@broker.task(retry_on_error=True)
async def notify_owner_text(text: str) -> None:
    """Служебное сообщение владелице (оплата после отмены, проблема с доставкой)."""
    chat_id = get_settings().owner.owner_chat_id
    if chat_id is None:
        logger.warning("OWNER_CHAT_ID не задан, сообщение владелице потеряно: %s", text)
        return
    try:
        await _send(chat_id, text)
    except TelegramForbiddenError:
        logger.error("Владелица не нажала /start в боте — уведомления ей не дойдут")


@broker.task(retry_on_error=True)
async def strip_order_buttons(order_id: int) -> None:
    """Снять кнопки «Оплатить» / «Отменить» с сообщений по оплаченному или отменённому заказу.

    Под текстом сообщения дописывается итог — «✅ Оплачено · заказ EM-1025» или
    «✖️ Заказ отменён», — чтобы по переписке было видно, чем всё закончилось.
    Удалять сообщения Telegram разрешает только 48 часов, а менять — всегда.
    """
    async with session_scope() as session:
        try:
            order = await orders.get_order(session, order_id)
        except NotFoundError:
            return
        messages = list(order.bot_messages or [])
        footer = _order_outcome(order)

    if not messages:
        return

    bot = get_bot()
    for entry in messages:
        chat_id, message_id = int(entry["chat_id"]), int(entry["message_id"])
        text = entry.get("text")
        try:
            if text and footer:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=f"{text}\n\n{footer}",
                    reply_markup=None,
                    disable_web_page_preview=True,
                )
            else:
                await bot.edit_message_reply_markup(
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=None,
                )
        except (TelegramBadRequest, TelegramForbiddenError) as error:
            # Кнопок уже нет, сообщение удалено или бот заблокирован — снимать нечего.
            logger.debug("Кнопки у сообщения %s не сняты: %s", message_id, error)

    async with session_scope() as session:
        order = await orders.get_order(session, order_id)
        # Оставляем только то, что появилось, пока мы снимали кнопки.
        order.bot_messages = [entry for entry in order.bot_messages if entry not in messages]


def _order_outcome(order: Order) -> str | None:
    """Чем закончился неоплаченный заказ — строка под сообщением с кнопкой оплаты."""
    if order.paid_at is not None:
        return f"✅ Оплачено · заказ №{order.number}"
    if order.status == OrderStatus.CANCELLED:
        return "✖️ Заказ отменён"
    return None


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
