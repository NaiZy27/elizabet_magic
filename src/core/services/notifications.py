"""Уведомления клиенту: тексты, подстановка и постановка задачи на отправку.

Шаблоны правятся в админке, поэтому рендер идёт в песочнице Jinja2 и с включённым
StrictUndefined: опечатка в плейсхолдере видна сразу при сохранении, а не в момент,
когда клиенту уходит «Заказ  готов».

Само сообщение отправляет фоновая задача и только после успешного коммита: запись
в базе можно откатить, отправленное сообщение — нет.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from jinja2 import StrictUndefined, TemplateError
from jinja2.sandbox import SandboxedEnvironment
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.clock import format_date, format_datetime
from core.db import after_commit
from core.enums import MessageTemplateKey
from core.errors import ValidationError
from core.models import MessageTemplate, Order, OrderItem
from core.money import format_rubles

logger = logging.getLogger(__name__)

_environment = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)

#: Плейсхолдеры, доступные в любом шаблоне. Подсказка к ним показывается в админке.
PLACEHOLDERS: dict[str, str] = {
    "order_number": "Номер заказа, например EM-1025. До оплаты — «на 1 700 ₽»",
    "customer_name": "Имя клиента",
    "email": "Email, на который уходит чек",
    "items": "Состав заказа списком",
    "box_count": "Сколько боксов в заказе",
    "subtotal": "Стоимость боксов и услуг",
    "delivery": "Стоимость доставки",
    "total": "Итоговая сумма",
    "has_video": "Заказано ли видео сборки (да/нет)",
    "receipt_url": "Ссылка на чек в «Мой налог»",
    "ready_date": "Дата готовности, обещанная клиенту",
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
        "Оплата получена! Ваш заказ №{{ order_number }} принят 💗\n\n"
        "Планируем собрать к {{ ready_date }} и сразу передадим в доставку.\n"
        "Чек придёт на {{ email }}."
    ),
    MessageTemplateKey.RECEIPT_ISSUED: (
        "Чек по заказу {{ order_number }} сформирован: {{ receipt_url }}"
    ),
    MessageTemplateKey.ORDER_REFUNDED: (
        "Мы вернули оплату по заказу {{ order_number }} — {{ total }}.\n"
        "Деньги придут на карту в течение нескольких дней, чек аннулирован."
    ),
    MessageTemplateKey.STATUS_ASSEMBLING: (
        "Собираем ваш бокс 🎀\nЗаказ {{ order_number }} уже в работе."
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
        "Ваш бокс приехал в пункт выдачи 🎁\n"
        "{{ pickup_address }}\n"
        "Режим работы: {{ pickup_working_hours }}"
    ),
    MessageTemplateKey.STATUS_COMPLETED: (
        "Спасибо, что выбрали нас 💗 Очень надеемся, что бокс понравился!"
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
        "items": ("1. Маленький бокс — 3 ложечки + видео сборки\n2. Маленький бокс — 2 ложечки"),
        "box_count": 2,
        "subtotal": format_rubles(240000),
        "delivery": format_rubles(30000),
        "total": format_rubles(270000),
        "has_video": "да",
        "receipt_url": "https://lknpd.nalog.ru/api/v1/receipt/000000000000/200abc/print",
        "ready_date": "22.09.2026",
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
    """Значения плейсхолдеров для конкретного заказа."""
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
    return context


def describe_item(item: OrderItem) -> str:
    """Один бокс строкой: «Маленький бокс — 3 ложечки + видео сборки»."""
    text = item.name_snapshot
    if item.quantity > 1:
        text = f"{text} × {item.quantity}"
    for addon in item.addons:
        text = f"{text} + {addon.name_snapshot.lower()}"
    return text


def describe_items(order: Order) -> str:
    """Состав заказа строками. Если боксов несколько — с номерами, как в корзине."""
    items = list(order.items)
    if len(items) == 1:
        lines = [describe_item(items[0])]
    else:
        lines = [f"{index}. {describe_item(item)}" for index, item in enumerate(items, start=1)]
    lines.extend(addon.name_snapshot for addon in order.order_addons)
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
    template = await get_template(session, key)
    text = template.text if template is not None else DEFAULT_TEMPLATES.get(key)
    if template is not None and not template.is_enabled:
        return None
    if not text:
        return None
    return render(text, build_context(order, **extra))


def schedule_customer_notification(
    session: AsyncSession,
    order: Order,
    key: MessageTemplateKey,
    **extra: Any,
) -> None:
    """Поставить отправку уведомления в очередь — после успешного коммита."""
    order_id = order.id
    payload = _jsonable(extra)

    async def _enqueue() -> None:
        # Импорт внутри: core не должен зависеть от worker на уровне модуля.
        from worker.tasks.notify import notify_customer

        await notify_customer.kiq(order_id=order_id, template_key=key.value, extra=payload)

    after_commit(session, _enqueue)


def schedule_owner_notification(session: AsyncSession, order: Order) -> None:
    """Сообщить владелице о новом оплаченном заказе."""
    order_id = order.id

    async def _enqueue() -> None:
        from worker.tasks.notify import notify_owner

        await notify_owner.kiq(order_id=order_id)

    after_commit(session, _enqueue)


def schedule_owner_text(session: AsyncSession, text: str) -> None:
    """Служебное сообщение владелице — о том, что требует её внимания."""

    async def _enqueue() -> None:
        from worker.tasks.notify import notify_owner_text

        await notify_owner_text.kiq(text=text)

    after_commit(session, _enqueue)


def schedule_strip_buttons(session: AsyncSession, order: Order) -> None:
    """Снять кнопки «Оплатить» / «Отменить» со всех сообщений бота по заказу."""
    if not order.bot_messages:
        return
    order_id = order.id

    async def _enqueue() -> None:
        from worker.tasks.notify import strip_order_buttons

        await strip_order_buttons.kiq(order_id=order_id)

    after_commit(session, _enqueue)


def _jsonable(extra: dict[str, Any]) -> dict[str, Any]:
    """Привести добавки к тому, что переживёт сериализацию задачи в Redis."""
    payload: dict[str, Any] = {}
    for name, value in extra.items():
        if isinstance(value, dt.datetime | dt.date):
            payload[name] = value.isoformat()
        elif isinstance(value, str | int | float | bool | None.__class__):
            payload[name] = value
        else:
            payload[name] = str(value)
    return payload
