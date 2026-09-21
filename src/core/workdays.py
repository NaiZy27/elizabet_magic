"""Рабочий календарь и расчёт дат готовности.

Чистая математика без базы: очередь сюда приходит списком, обратно уходят даты.
Работа с самой очередью — в core.services.board.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field

from core.clock import today_moscow
from core.enums import OrderStatus
from core.errors import ValidationError

#: По умолчанию мастерская работает по будням. Меняется в настройках админки.
DEFAULT_WORKING_WEEKDAYS: frozenset[int] = frozenset({0, 1, 2, 3, 4})

#: Предохранитель от бесконечного поиска рабочего дня, если календарь задан странно.
_MAX_DAYS_LOOKAHEAD = 366


@dataclass(frozen=True, slots=True)
class WorkingCalendar:
    """Когда владелица собирает боксы: дни недели и отдельные выходные даты."""

    weekdays: frozenset[int] = DEFAULT_WORKING_WEEKDAYS
    #: Отпуск и праздники — конкретные даты, в которые сборки нет.
    holidays: frozenset[dt.date] = field(default_factory=frozenset)

    def is_working(self, day: dt.date) -> bool:
        return day.weekday() in self.weekdays and day not in self.holidays


@dataclass(frozen=True, slots=True)
class QueueEntry:
    """Заказ в очереди — ровно то, что нужно для расчёта дат."""

    order_id: int
    production_days: int
    status: OrderStatus
    queue_position: int


def ensure_working_day(day: dt.date, calendar: WorkingCalendar) -> dt.date:
    """Сам день, если он рабочий, иначе ближайший следующий рабочий."""
    if not calendar.weekdays:
        raise ValidationError("В настройках не отмечен ни один рабочий день недели")
    candidate = day
    for _ in range(_MAX_DAYS_LOOKAHEAD):
        if calendar.is_working(candidate):
            return candidate
        candidate += dt.timedelta(days=1)
    raise ValidationError(
        "Не удалось найти рабочий день на год вперёд — проверьте выходные в настройках",
    )


def next_working_day(day: dt.date, calendar: WorkingCalendar) -> dt.date:
    """Ближайший рабочий день строго после указанного."""
    return ensure_working_day(day + dt.timedelta(days=1), calendar)


def shift_working_days(start: dt.date, days: int, calendar: WorkingCalendar) -> dt.date:
    """Сдвинуть дату на N рабочих дней вперёд. Отсчёт начинается с рабочего дня."""
    if days < 0:
        raise ValidationError("Срок изготовления не может быть отрицательным")
    current = ensure_working_day(start, calendar)
    for _ in range(days):
        current = next_working_day(current, calendar)
    return current


def compute_ready_dates(
    entries: Sequence[QueueEntry],
    *,
    calendar: WorkingCalendar,
    today: dt.date | None = None,
) -> dict[int, dt.date]:
    """Расчётные даты готовности для всей очереди.

    Заказы идут строго по queue_position: первый начинается сегодня (или в ближайший
    рабочий день), каждый следующий — после того, как закончится предыдущий.
    """
    cursor = ensure_working_day(today or today_moscow(), calendar)
    ready_dates: dict[int, dt.date] = {}
    for entry in sorted(entries, key=lambda item: item.queue_position):
        # Заказ на один день готов в тот же день, на два — на следующий рабочий.
        ready = shift_working_days(cursor, max(1, entry.production_days) - 1, calendar)
        ready_dates[entry.order_id] = ready
        cursor = next_working_day(ready, calendar)
    return ready_dates


def is_late(ready_date: dt.date, promised_date: dt.date | None) -> bool:
    """Не успеваем к дате, которую назвали клиенту."""
    return promised_date is not None and ready_date > promised_date
