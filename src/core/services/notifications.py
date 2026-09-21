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
from core.enums import VIDEO_ADDON_CODE, MessageTemplateKey
from core.errors import ValidationError
from core.models import MessageTemplate, Order
from core.money import format_rubles
from core.text import spoons_phrase

logger = logging.getLogger(__name__)

_environment = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)

#: Плейсхолдеры, доступные в любом шаблоне. Подсказка к ним показывается в админке.
PLACEHOLDERS: dict[str, str] = {
    "order_number": "Номер заказа, например EM-1025",
    "customer_name": "Имя клиента",
    "items": "Состав заказа списком",
    "box_count": "Сколько боксов в заказе",
    "subtotal": "Стоимость боксов и услуг",
    "delivery": "Стоимость доставки",
    "total": "Итоговая сумма",
    "has_video": "Заказано ли видео сборки (да/нет)",
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
        "Чтобы мы начали сборку, оплатите заказ — ссылка действует до {{ pay_deadline }}."
    ),
    MessageTemplateKey.ORDER_PAID: (
        "Оплата получена, заказ {{ order_number }} принят в работу 💗\n\n"
        "Планируем собрать к {{ ready_date }} и сразу передадим в доставку.\n"
        "Чек придёт на указанный вами телефон или email."
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
        "items": "Маленький бокс — 3 ложечки × 2\nВидео сборки",
        "box_count": 2,
        "subtotal": format_rubles(270000),
        "delivery": format_rubles(30000),
        "total": format_rubles(300000),
        "has_video": "да",
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
    """Заказано ли видео сборки — по снимку кода услуги в заказе."""
    return any(addon.code_snapshot == VIDEO_ADDON_CODE for addon in order.addons)


def build_context(order: Order, **extra: Any) -> dict[str, Any]:
    """Значения плейсхолдеров для конкретного заказа."""
    snapshot = order.pickup_snapshot or {}
    context: dict[str, Any] = {
        "order_number": order.display_number,
        "customer_name": order.recipient_name or "",
        "items": describe_items(order),
        "box_count": sum(item.quantity for item in order.items),
        "subtotal": format_rubles(order.subtotal_kopecks),
        "delivery": format_rubles(order.delivery_kopecks),
        "total": format_rubles(order.total_kopecks),
        "has_video": "да" if has_video(order) else "нет",
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


def describe_items(order: Order) -> str:
    """Состав заказа строками: «Маленький бокс — 3 ложечки × 2»."""
    lines = [
        f"{item.name_snapshot} × {item.quantity}" if item.quantity > 1 else item.name_snapshot
        for item in order.items
    ]
    lines.extend(
        f"{addon.name_snapshot} × {addon.quantity}"
        if addon.quantity > 1
        else addon.name_snapshot
        for addon in order.addons
    )
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
