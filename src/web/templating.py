"""Шаблоны админки и фильтры к ним.

Все форматы — русские: «20.09.2026 21:19», «3 900 ₽», подписи статусов словами.
В шаблонах не должно быть ни одного сырого кода и ни одной точки в цене.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates

from core import labels
from core.clock import format_date, format_date_short, format_datetime, format_datetime_short
from core.money import format_rubles
from core.text import pluralize, truncate

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _rubles(value: int | None) -> str:
    return format_rubles(value or 0)


def _rubles_input(value: int | None) -> str:
    """Копейки -> значение для поля ввода: 120000 -> «1200», 120050 -> «1200.50»."""
    kopecks = value or 0
    whole, rest = divmod(kopecks, 100)
    return str(whole) if rest == 0 else f"{whole}.{rest:02d}"


def _datetime(value: dt.datetime | None) -> str:
    return format_datetime(value) if value else "—"


def _datetime_short(value: dt.datetime | None) -> str:
    return format_datetime_short(value) if value else "—"


def _date(value: dt.date | None) -> str:
    return format_date(value) if value else "—"


def _date_short(value: dt.date | None) -> str:
    return format_date_short(value) if value else "—"


def _yes_no(value: Any) -> str:
    return "Да" if value else "Нет"


templates.env.filters.update(
    {
        "rubles": _rubles,
        "rubles_input": _rubles_input,
        "ru_datetime": _datetime,
        "ru_datetime_short": _datetime_short,
        "ru_date": _date,
        "ru_date_short": _date_short,
        "yes_no": _yes_no,
        "truncate_ru": truncate,
        "order_status": labels.order_status_label,
        "payment_status": labels.payment_status_label,
        "shipment_status": labels.shipment_status_label,
        "delivery_provider": labels.delivery_provider_label,
        "payment_provider": labels.payment_provider_label,
        "order_event": labels.order_event_label,
        "pluralize_ru": pluralize,
    },
)


def render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
):
    """Отрисовать страницу. `request` нужен шаблонам для url_for и текущего адреса."""
    payload: dict[str, Any] = {"request": request}
    payload.update(context or {})
    return templates.TemplateResponse(
        request=request,
        name=name,
        context=payload,
        status_code=status_code,
    )
