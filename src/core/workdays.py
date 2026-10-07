"""Рабочий календарь и расчёт дат готовности.

Чистая математика без базы: очередь сюда приходит списком, обратно уходят даты.
Работа с самой очередью — в core.services.board.
"""

from __future__ import annotations

import datetime as dt
import math
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
class DailyCapacity:
    """Сколько боксов мастерская собирает за день.

    «2 с видео или 4 без видео» значит: бокс с видео занимает половину дня, без видео —
    четверть. Поэтому за день можно собрать и 1 с видео + 2 без видео.
    """

    video_per_day: int = 2
    plain_per_day: int = 4

    @property
    def day_units(self) -> int:
        """Ёмкость дня в общих единицах: наименьшее общее кратное двух лимитов."""
        return math.lcm(self.video_per_day, self.plain_per_day)

    def box_units(self, *, with_video: bool) -> int:
        per_day = self.video_per_day if with_video else self.plain_per_day
        return self.day_units // per_day


@dataclass(frozen=True, slots=True)
class WorkingCalendar:
    """Когда владелица собирает боксы: дни недели и отдельные выходные даты."""

    weekdays: frozenset[int] = DEFAULT_WORKING_WEEKDAYS
    #: Отпуск и праздники — конкретные даты, в которые сборки нет.
    holidays: frozenset[dt.date] = field(default_factory=frozenset)
    #: Дневной лимит сборки. Пусто — заказы идут друг за другом по сроку изготовления.
    capacity: DailyCapacity | None = None

    def is_working(self, day: dt.date) -> bool:
        return day.weekday() in self.weekdays and day not in self.holidays


@dataclass(frozen=True, slots=True)
class QueueEntry:
    """Заказ в очереди — ровно то, что нужно для расчёта дат."""

    order_id: int
    production_days: int
    status: OrderStatus
    queue_position: int
    #: Сколько боксов снимаем на видео и сколько без — для дневного лимита сборки.
    video_boxes: int = 0
    plain_boxes: int = 0


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
    capacity: DailyCapacity | None = None,
) -> dict[int, dt.date]:
    """Расчётные даты готовности для всей очереди.

    Заказы идут строго по queue_position. С `capacity` боксы раскладываются по дням
    в пределах дневного лимита: заказ готов в тот день, куда лёг его последний бокс.
    Без него — по сроку изготовления: каждый следующий заказ после предыдущего.
    """
    capacity = capacity or calendar.capacity
    if capacity is not None:
        return _ready_dates_by_capacity(entries, calendar=calendar, today=today, capacity=capacity)

    cursor = ensure_working_day(today or today_moscow(), calendar)
    ready_dates: dict[int, dt.date] = {}
    for entry in sorted(entries, key=lambda item: item.queue_position):
        # Заказ на один день готов в тот же день, на два — на следующий рабочий.
        ready = shift_working_days(cursor, max(1, entry.production_days) - 1, calendar)
        ready_dates[entry.order_id] = ready
        cursor = next_working_day(ready, calendar)
    return ready_dates


def _ready_dates_by_capacity(
    entries: Sequence[QueueEntry],
    *,
    calendar: WorkingCalendar,
    today: dt.date | None,
    capacity: DailyCapacity,
) -> dict[int, dt.date]:
    day = ensure_working_day(today or today_moscow(), calendar)
    used = 0
    limit = capacity.day_units
    ready_dates: dict[int, dt.date] = {}
    for entry in sorted(entries, key=lambda item: item.queue_position):
        boxes = [True] * entry.video_boxes + [False] * entry.plain_boxes
        if not boxes:
            # Заказ без боксов в очереди быть не должен, но место он всё равно займёт.
            boxes = [False]
        for with_video in boxes:
            units = capacity.box_units(with_video=with_video)
            if used + units > limit:
                day = next_working_day(day, calendar)
                used = 0
            used += units
        ready_dates[entry.order_id] = day
    return ready_dates


def is_late(ready_date: dt.date, promised_date: dt.date | None) -> bool:
    """Не успеваем к дате, которую назвали клиенту."""
    return promised_date is not None and ready_date > promised_date
