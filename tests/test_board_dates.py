"""Расчётные даты готовности: очередь собирается последовательно, по рабочим дням."""

from __future__ import annotations

import datetime as dt

import pytest

from core.enums import OrderStatus
from core.errors import ValidationError
from core.workdays import (
    QueueEntry,
    WorkingCalendar,
    compute_ready_dates,
    ensure_working_day,
    is_late,
    next_working_day,
    shift_working_days,
)


def entry(order_id: int, position: int, production_days: int = 1) -> QueueEntry:
    return QueueEntry(
        order_id=order_id,
        production_days=production_days,
        status=OrderStatus.QUEUED,
        queue_position=position,
    )


def test_one_day_order_is_ready_today(workdays, monday):
    dates = compute_ready_dates([entry(1, 1)], calendar=workdays, today=monday)

    assert dates == {1: monday}


def test_orders_follow_each_other(workdays, monday):
    dates = compute_ready_dates(
        [entry(1, 1), entry(2, 2, production_days=2), entry(3, 3)],
        calendar=workdays,
        today=monday,
    )

    assert dates[1] == dt.date(2026, 9, 21)  # понедельник
    assert dates[2] == dt.date(2026, 9, 23)  # начат во вторник, два дня — по среду
    assert dates[3] == dt.date(2026, 9, 24)  # четверг


def test_weekend_is_skipped(workdays):
    friday = dt.date(2026, 9, 25)

    dates = compute_ready_dates([entry(1, 1), entry(2, 2)], calendar=workdays, today=friday)

    assert dates[1] == friday
    assert dates[2] == dt.date(2026, 9, 28)  # следующий понедельник


def test_start_moves_to_next_working_day(workdays):
    saturday = dt.date(2026, 9, 26)

    dates = compute_ready_dates([entry(1, 1)], calendar=workdays, today=saturday)

    assert dates[1] == dt.date(2026, 9, 28)


def test_holidays_push_dates(monday):
    calendar = WorkingCalendar(holidays=frozenset({dt.date(2026, 9, 22)}))

    dates = compute_ready_dates(
        [entry(1, 1), entry(2, 2)],
        calendar=calendar,
        today=monday,
    )

    assert dates[1] == monday
    assert dates[2] == dt.date(2026, 9, 23)  # вторник — выходной, значит среда


def test_queue_position_defines_order(workdays, monday):
    # Порядок в списке не важен: считаем по позиции в очереди.
    dates = compute_ready_dates(
        [entry(2, 2, production_days=2), entry(1, 1)],
        calendar=workdays,
        today=monday,
    )

    assert dates[1] == monday
    assert dates[2] == dt.date(2026, 9, 23)


def test_working_day_helpers(workdays):
    friday = dt.date(2026, 9, 25)

    assert ensure_working_day(friday, workdays) == friday
    assert ensure_working_day(dt.date(2026, 9, 26), workdays) == dt.date(2026, 9, 28)
    assert next_working_day(friday, workdays) == dt.date(2026, 9, 28)
    assert shift_working_days(friday, 0, workdays) == friday
    assert shift_working_days(friday, 3, workdays) == dt.date(2026, 9, 30)


def test_calendar_without_working_days_is_rejected(monday):
    calendar = WorkingCalendar(weekdays=frozenset())

    with pytest.raises(ValidationError, match="рабочий день"):
        ensure_working_day(monday, calendar)


def test_is_late_compares_with_promise(monday):
    assert is_late(dt.date(2026, 9, 23), monday) is True
    assert is_late(monday, monday) is False
    assert is_late(monday, None) is False
