"""Отправка заказа: данные для владелицы.

Все службы — СДЭК, Яндекс Доставка, Ozon — ручные: отправку владелица оформляет
сама в приложении службы. Бот присылает ей всё нужное для этого:
- в карточке нового оплаченного заказа — блок «Доставка»;
- когда заказ переходит в «Готов», — отдельное сообщение: служба, пункт с кодом,
  получатель и телефон (тапом копируются), состав заказа.

Этапы «Передан в доставку», «Доставлен в ПВЗ» и «Получен» владелица ставит сама.
"""

from __future__ import annotations

from html import escape
from typing import Any

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Order
from core.services import notifications
from core.text import phone_pretty

#: Пометка для адреса, который клиент ввёл сам, а не выбрал из справочника.
MANUAL_NOTE = "✍️ Адрес клиент ввёл сам — найдите пункт в приложении службы"


def _point(order: Order) -> dict[str, Any]:
    return order.pickup_snapshot or {}


def recipient_line(order: Order) -> str:
    """«Анна Иванова · +7 900 123-45-67»."""
    parts = [escape(order.recipient_name or "—")]
    if order.recipient_phone:
        parts.append(f"<code>{escape(phone_pretty(order.recipient_phone))}</code>")
    return " · ".join(parts)


def telegram_line(order: Order) -> str | None:
    """Telegram покупателя для связи: «@anna» или ссылка на профиль, если ника нет."""
    # Покупатель мог быть не загружен вместе с заказом: в асинхронном коде ленивая
    # подгрузка упала бы, поэтому без него просто не пишем строку.
    if "customer" in inspect(order).unloaded:
        return None
    customer = order.customer
    if customer is None:
        return None
    if customer.username:
        return f"@{escape(customer.username)}"
    name = escape(customer.full_name or "профиль покупателя")
    return f'<a href="tg://user?id={customer.telegram_user_id}">{name}</a> (без ника)'


def point_line(order: Order) -> str:
    """«Москва, Нагорная ул., 15 · код 2500» — адрес и код копируются тапом."""
    point = _point(order)
    if point.get("manual"):
        # Адрес клиент написал сам — показываем как есть, город в нём уже есть.
        return f"<code>{escape(str(point.get('address') or '—'))}</code>"
    address = ", ".join(part for part in (point.get("city"), point.get("address")) if part)
    line = f"<code>{escape(address or '—')}</code>"
    if point.get("external_id"):
        line += f" · код <code>{escape(str(point['external_id']))}</code>"
    return line


def owner_delivery_lines(order: Order) -> list[str]:
    """Блок «Доставка» в карточке нового заказа для владелицы."""
    point = _point(order)
    provider = escape(str(point.get("provider_name") or "—"))
    lines = [
        f"🚚 {provider} · отправить вручную, когда заказ будет готов",
        f"📍 {point_line(order)}",
    ]
    if point.get("manual"):
        lines.append(MANUAL_NOTE)
    lines.append(f"👤 {recipient_line(order)}")
    telegram = telegram_line(order)
    if telegram:
        lines.append(f"💬 {telegram}")
    return lines


def manual_dispatch_text(order: Order) -> str:
    """Всё, что нужно владелице, чтобы оформить отправку в приложении службы."""
    point = _point(order)
    provider = escape(str(point.get("provider_name") or "службу доставки"))
    label = escape(order.admin_label)
    lines = [
        f"📦 <b>Заказ {label} готов — отправьте через {provider}</b>",
        "",
        f"📍 Пункт: {point_line(order)}",
    ]
    if point.get("manual"):
        lines.append(MANUAL_NOTE)
    if point.get("working_hours"):
        lines.append(f"🕘 {escape(str(point['working_hours']))}")
    lines.append(f"👤 Получатель: {recipient_line(order)}")
    telegram = telegram_line(order)
    if telegram:
        lines.append(f"💬 Telegram: {telegram}")
    lines += [
        "",
        escape(notifications.describe_items(order)),
    ]
    return "\n".join(lines)


def on_ready(session: AsyncSession, order: Order) -> None:
    """Заказ собран — владелице данные для отправки. Уходят после коммита."""
    if order.pickup_snapshot:
        notifications.schedule_owner_text(session, manual_dispatch_text(order))
