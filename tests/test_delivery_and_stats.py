"""Чистые функции доставки и статистики — без базы."""

from __future__ import annotations

import datetime as dt

import pytest

from core.services.delivery import DEFAULT_PARCEL, Parcel, distance_km, fits
from core.services.stats import period_range


def test_distance_between_known_points():
    # Москва — Санкт-Петербург, примерно 634 км по прямой.
    km = distance_km(55.7558, 37.6173, 59.9311, 30.3609)
    assert 620 < km < 650


def test_distance_to_itself_is_zero():
    assert distance_km(55.75, 37.61, 55.75, 37.61) == pytest.approx(0.0, abs=1e-6)


def test_short_distance_is_small():
    # Два адреса в пределах одного города.
    km = distance_km(55.7649, 37.6055, 55.7494, 37.5911)
    assert 0 < km < 3


def test_parcel_fits_when_point_has_no_limit():
    point = type("Point", (), {"max_weight_g": None})()
    assert fits(point, DEFAULT_PARCEL) is True


def test_parcel_does_not_fit_heavy_point_limit():
    point = type("Point", (), {"max_weight_g": 300})()
    heavy = Parcel(weight_g=500, length_cm=20, width_cm=15, height_cm=10)

    assert fits(point, heavy) is False


def test_period_range_today():
    since, until = period_range("today", since=None, until=None)
    assert since == until


def test_period_range_week_covers_seven_days():
    since, until = period_range("week", since=None, until=None)
    assert (until - since).days == 6


def test_period_range_custom_is_sorted():
    first = dt.date(2026, 9, 10)
    last = dt.date(2026, 9, 1)

    since, until = period_range("custom", since=first, until=last)

    assert since == last
    assert until == first


def test_period_range_falls_back_to_month():
    since, until = period_range("что-то непонятное", since=None, until=None)
    assert (until - since).days == 29
