"""Время.

В базе — только timestamptz в UTC. Московское время существует только при показе
человеку и при расчёте рабочих дней (очередь и даты готовности живут по Москве).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, tzinfo
from functools import lru_cache
from zoneinfo import ZoneInfo


@lru_cache(maxsize=1)
def moscow() -> tzinfo:
    """Московская зона.

    Берётся лениво: база часовых поясов приезжает пакетом tzdata, и падать
    на импорте модуля из-за неё не стоит.
    """
    return ZoneInfo("Europe/Moscow")


def now_utc() -> datetime:
    """Текущий момент с таймзоной. Наивного времени в проекте нет."""
    return datetime.now(UTC)


def to_moscow(value: datetime) -> datetime:
    """Момент из базы -> московское время для показа.

    Наивное значение считаем UTC: так ведут себя драйверы, если колонку
    случайно объявили без таймзоны.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(moscow())


def today_moscow() -> date:
    """Сегодняшняя дата по Москве — точка отсчёта для дат готовности."""
    return now_utc().astimezone(moscow()).date()


def format_datetime(value: datetime) -> str:
    """«20.09.2026 21:19»."""
    return to_moscow(value).strftime("%d.%m.%Y %H:%M")


def format_datetime_short(value: datetime) -> str:
    """«20.09, 21:19» — для карточек на доске."""
    return to_moscow(value).strftime("%d.%m, %H:%M")


def format_date(value: date) -> str:
    """«20.09.2026»."""
    return value.strftime("%d.%m.%Y")


def format_date_short(value: date) -> str:
    """«20.09» — когда год очевиден из контекста."""
    return value.strftime("%d.%m")
