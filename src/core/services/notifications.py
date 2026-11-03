"""Уведомления клиенту: тексты, подстановка и постановка задачи на отправку.

Шаблоны правятся в админке, поэтому рендер идёт в песочнице Jinja2 и с включённым
StrictUndefined: опечатка в плейсхолдере видна сразу при сохранении, а не в момент,
когда клиенту уходит «Заказ  готов».

Само сообщение отправляет фоновая задача и только после успешного коммита: запись
в базе можно откатить, отправленное сообщение — нет (см. core.services.outbox).

Сообщения уходят в Telegram как HTML. Шаблон пишет владелица и может ставить в нём
теги (<b>), а вот подставленные значения — адрес, имя, название бокса — экранируются:
«<» в адресе, который клиент ввёл сам, иначе ломал бы всё сообщение.
"""

from __future__ import annotations

import datetime as dt
import html
import logging
from typing import Any

from jinja2 import StrictUndefined, TemplateError
from jinja2.sandbox import SandboxedEnvironment
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.clock import format_date, format_datetime
from core.enums import MessageTemplateKey, OutboxKind
from core.errors import ValidationError
from core.models import MessageTemplate, Order, OrderItem
from core.money import format_rubles
from core.services import outbox

logger = logging.getLogger(__name__)

_environment = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)

#: Плейсхолдеры, доступные в любом шаблоне. Подсказка к ним показывается в админке.
PLACEHOLDERS: dict[str, str] = {
    "order_number": "Номер заказа, например EM-1025. До оплаты — «на 1 700 ₽»",
    "customer_name": "Имя клиента",
    "email": "Email, на который уходит чек",
    "items": "Состав заказа списком",
    "box_count": "Сколько позиций в заказе",
    "subtotal": "Стоимость ложечек и услуг",
    "delivery": "Стоимость доставки",
    "total": "Итоговая сумма",
    "has_video": "Заказано ли видео сборки (да/нет)",
    "receipt_url": "Ссылка на чек в «Мой налог»",
    "ready_date": "Дата готовности, обещанная клиенту",
    "queue_position": "Место заказа в очереди сборки",
    "delivery_service": "Служба доставки",
    "pickup_name": "Название пункта выдачи",
    "pickup_address": "Адрес пункта выдачи",
    "pickup_city": "Город",
    "pickup_working_hours": "Режим работы пункта выдачи",
    "track": "Трек-номер отправления",
    "pay_url": "Ссылка на оплату",
    "pay_deadline": "До какого времени действует ссылка на оплату",
}

#: Тексты по умолчанию. Заводятся при первом запуске и дальше правятся в админке.
DEFAULT_TEMPLATES: dict[MessageTemplateKey, str] = {
    MessageTemplateKey.ORDER_CREATED: (
        "Спасибо! Заказ {{ order_number }} оформлен 💗\n\n"
        "{{ items }}\n\n"
        "Доставка: {{ delivery }}\n"
        "Итого: {{ total }}\n\n"
        "Чтобы мы начали сборку, оплатите заказ — ссылка действует до {{ pay_deadline }}.\n"
        "Номер заказа пришлём сразу после оплаты."
    ),
    MessageTemplateKey.ORDER_PAID: (
        "Оплата получена 💗 Заказ №{{ order_number }}\n\n"
        "Место в очереди: {{ queue_position }}\n"
        "Соберём к {{ ready_date }} и сразу отправим.\n"
        "Чек придёт на {{ email }}."
    ),
    MessageTemplateKey.RECEIPT_ISSUED: (
        "Чек по заказу {{ order_number }} сформирован: {{ receipt_url }}"
    ),
    MessageTemplateKey.STATUS_ASSEMBLING: (
        "Собираем ваши ложечки 🎀\nЗаказ {{ order_number }} уже в работе."
    ),
    MessageTemplateKey.STATUS_READY: (
        "Заказ {{ order_number }} готов! Отвезём его в службу доставки в ближайшее время."
    ),
    MessageTemplateKey.STATUS_SHIPPED: (
        "Заказ {{ order_number }} передан в доставку ({{ delivery_service }}).\n"
        "Пункт выдачи: {{ pickup_address }}."
    ),
    MessageTemplateKey.TRACK_ASSIGNED: (
        "Заказ {{ order_number }} в пути.\nНомер для отслеживания: {{ track }}"
    ),
    MessageTemplateKey.ARRIVED_AT_PICKUP: (
        "Ваш заказ приехал в пункт выдачи 🎁\n"
        "{{ pickup_address }}\n"
        "Режим работы: {{ pickup_working_hours }}"
    ),
    MessageTemplateKey.STATUS_COMPLETED: (
        "Спасибо, что выбрали нас 💗 Очень надеемся, что ложечки понравились!"
    ),
    MessageTemplateKey.ORDER_CANCELLED: (
        "Заказ {{ order_number }} отменён.\n"
        "Если это вышло случайно — напишите нам, соберём его заново."
    ),
}


def render(text: str, context: dict[str, Any]) -> str:
    """Подставить значения в шаблон."""
    try:
        return _environment.from_string(text).render(**context).strip()
    except TemplateError as error:
        raise ValidationError(f"Ошибка в тексте сообщения: {error}") from error


def sample_context() -> dict[str, Any]:
    """Образцовые значения — для предпросмотра и проверки шаблона при сохранении."""
    return {
        "order_number": "EM-1025",
        "customer_name": "Анна",
        "email": "anna@mail.ru",
        "items": ("1. Маленькие ложечки — 3 шт. + видео сборки\n2. Большие ложечки — 2 шт."),
        "box_count": 2,
        "subtotal": format_rubles(240000),
        "delivery": format_rubles(30000),
        "total": format_rubles(270000),
        "has_video": "да",
        "receipt_url": "https://lknpd.nalog.ru/api/v1/receipt/000000000000/200abc/print",
        "ready_date": "22.09.2026",
        "queue_position": 3,
        "delivery_service": "СДЭК",
        "pickup_name": "Пункт выдачи на Ленина",
        "pickup_address": "г. Казань, ул. Ленина, 15",
        "pickup_city": "Казань",
        "pickup_working_hours": "Пн-Пт 10:00–20:00",
        "track": "1234567890",
        "pay_url": "https://example.ru/pay/abc",
        "pay_deadline": "21.09.2026 22:19",
    }


def validate_template(text: str) -> str:
    """Проверить шаблон перед сохранением и вернуть предпросмотр.

    Неизвестный плейсхолдер — ошибка: иначе клиент получит сообщение с дыркой.
    """
    if not text.strip():
        raise ValidationError("Текст сообщения не может быть пустым")
    try:
        preview = _environment.from_string(text).render(**sample_context()).strip()
    except TemplateError as error:
        known = ", ".join(sorted(PLACEHOLDERS))
        raise ValidationError(
            f"Не получилось собрать сообщение: {error}. Доступные вставки: {known}",
        ) from error
    return preview


def has_video(order: Order) -> bool:
    """Заказано ли видео сборки хотя бы для одного бокса."""
    return order.video_box_count > 0


def build_context(order: Order, **extra: Any) -> dict[str, Any]:
    """Значения плейсхолдеров для конкретного заказа, уже экранированные для HTML."""
    snapshot = order.pickup_snapshot or {}
    context: dict[str, Any] = {
        "order_number": order.display_number,
        "customer_name": order.recipient_name or "",
        "email": order.recipient_email or "ваш email",
        "items": describe_items(order),
        "box_count": order.box_count,
        "subtotal": format_rubles(order.subtotal_kopecks),
        "delivery": format_rubles(order.delivery_kopecks),
        "total": format_rubles(order.total_kopecks),
        "has_video": "да" if has_video(order) else "нет",
        "receipt_url": "",
        "ready_date": (
            format_date(order.promised_ready_date) if order.promised_ready_date else "уточняется"
        ),
        "queue_position": order.queue_position or "—",
        "delivery_service": snapshot.get("provider_name", ""),
        "pickup_name": snapshot.get("name", ""),
        "pickup_address": snapshot.get("address", ""),
        "pickup_city": snapshot.get("city", ""),
        "pickup_working_hours": snapshot.get("working_hours", ""),
        "track": "",
        "pay_url": "",
        "pay_deadline": format_datetime(order.expires_at) if order.expires_at else "",
    }
    context.update(extra)
    # Состав уже собран с экранированием — второй раз «&» превратился бы в «&amp;amp;».
    return {
        name: html.escape(value, quote=False)
        if isinstance(value, str) and name != "items"
        else value
        for name, value in context.items()
    }


def describe_item(item: OrderItem) -> str:
    """Один бокс строкой для Telegram-HTML: «Маленький бокс — 3 ложечки + видео сборки»."""
    text = html.escape(item.name_snapshot, quote=False)
    if item.quantity > 1:
        text = f"{text} × {item.quantity}"
    for addon in item.addons:
        text = f"{text} + {html.escape(addon.name_snapshot.lower(), quote=False)}"
    return text


def describe_items(order: Order) -> str:
    """Состав заказа строками, готовыми для Telegram-HTML. Несколько боксов — с номерами."""
    items = list(order.items)
    if len(items) == 1:
        lines = [describe_item(items[0])]
    else:
        lines = [f"{index}. {describe_item(item)}" for index, item in enumerate(items, start=1)]
    lines.extend(html.escape(addon.name_snapshot, quote=False) for addon in order.order_addons)
    return "\n".join(lines)


async def get_template(
    session: AsyncSession,
    key: MessageTemplateKey,
) -> MessageTemplate | None:
    return await session.scalar(select(MessageTemplate).where(MessageTemplate.key == key.value))


async def render_for_order(
    session: AsyncSession,
    order: Order,
    key: MessageTemplateKey,
    **extra: Any,
) -> str | None:
    """Готовый текст уведомления или None, если шаблон выключен в админке."""
    text = await template_text(session, key)
    return render(text, build_context(order, **extra)) if text else None


async def template_text(session: AsyncSession, key: MessageTemplateKey) -> str | None:
    """Текст шаблона из админки (или стандартный). None — шаблон выключен."""
    template = await get_template(session, key)
    if template is not None and not template.is_enabled:
        return None
    return (template.text if template is not None else DEFAULT_TEMPLATES.get(key)) or None


def schedule_customer_notification(
    session: AsyncSession,
    order: Order,
    key: MessageTemplateKey,
    **extra: Any,
) -> None:
    """Поставить уведомление клиенту в очередь. Уйдёт после успешного коммита.

    Значения для текста снимаются сейчас, а не при отправке: если воркер лежал и
    сообщения ушли позже, клиент получит «собирается» с данными того момента, а не
    с пунктом или датой, которые успели поменяться.
    """
    context = _jsonable(build_context(order, **extra))
    outbox.enqueue(
        session,
        OutboxKind.CUSTOMER,
        {"order_id": order.id, "template": key.value, "context": context},
    )


def schedule_owner_notification(session: AsyncSession, order: Order) -> None:
    """Сообщить владелице о новом оплаченном заказе."""
    outbox.enqueue(session, OutboxKind.OWNER_ORDER, {"order_id": order.id})


def schedule_owner_text(session: AsyncSession, text: str) -> None:
    """Служебное сообщение владелице — о том, что требует её внимания."""
    outbox.enqueue(session, OutboxKind.OWNER_TEXT, {"text": text})


def schedule_alert(session: AsyncSession, text: str) -> None:
    """Техническая тревога — в чат ALERT_CHAT_ID (по умолчанию владелице)."""
    outbox.enqueue(session, OutboxKind.ALERT, {"text": text})


def schedule_strip_buttons(session: AsyncSession, order: Order) -> None:
    """Снять кнопки «Оплатить» / «Отменить» со всех сообщений бота по заказу."""
    if not order.bot_messages:
        return
    outbox.enqueue(session, OutboxKind.STRIP_BUTTONS, {"order_id": order.id})


def _jsonable(extra: dict[str, Any]) -> dict[str, Any]:
    """Привести значения к тому, что переживёт сохранение в JSONB."""
    payload: dict[str, Any] = {}
    for name, value in extra.items():
        if isinstance(value, dt.datetime | dt.date):
            payload[name] = value.isoformat()
        elif isinstance(value, str | int | float | bool | None.__class__):
            payload[name] = value
        else:
            payload[name] = str(value)
    return payload
