"""Цена бокса: варианты из каталога и докупка ложечек сверх самого большого."""

from __future__ import annotations

import pytest

from core.errors import ValidationError
from core.services.catalog import (
    compute_production_days,
    item_name,
    largest_variant,
    price_for_spoons,
    spoon_options,
)
from tests.conftest import add_variant, make_addon, make_product


def test_price_matches_variant(small_box):
    variant, price = price_for_spoons(small_box, small_box.variants, 2)

    assert price == 90000
    assert variant.spoon_count == 2


def test_extra_spoons_counted_from_largest_variant(small_box):
    # 3 ложечки — 1200 ₽, каждая следующая — по 300 ₽.
    base, price = price_for_spoons(small_box, small_box.variants, 5)

    assert base.spoon_count == 3
    assert price == 120000 + 2 * 30000


def test_extra_spoons_limited_by_max(small_box):
    _, price = price_for_spoons(small_box, small_box.variants, 6)
    assert price == 120000 + 3 * 30000

    with pytest.raises(ValidationError, match="7 ложечек"):
        price_for_spoons(small_box, small_box.variants, 7)


def test_extra_spoons_disabled_without_price():
    product = make_product(name="Большой бокс")
    add_variant(product, spoon_count=5, price_kopecks=300000)

    with pytest.raises(ValidationError):
        price_for_spoons(product, product.variants, 6)


def test_inactive_variant_is_not_offered():
    product = make_product()
    add_variant(product, spoon_count=1, price_kopecks=50000)
    add_variant(product, spoon_count=2, price_kopecks=90000, is_active=False)

    assert [variant.spoon_count for variant in product.variants if variant.is_active] == [1]
    with pytest.raises(ValidationError):
        price_for_spoons(product, product.variants, 2)


def test_spoon_options_include_extra_range(small_box):
    assert spoon_options(small_box, small_box.variants) == [1, 2, 3, 4, 5, 6]


def test_spoon_options_without_extra():
    product = make_product(name="Средний бокс")
    add_variant(product, spoon_count=3, price_kopecks=200000)
    add_variant(product, spoon_count=5, price_kopecks=300000)

    assert spoon_options(product, product.variants) == [3, 5]


def test_largest_variant_ignores_disabled(small_box):
    small_box.variants[-1].is_active = False

    biggest = largest_variant(small_box.variants)

    assert biggest is not None
    assert biggest.spoon_count == 2


def test_item_name_snapshot(small_box):
    assert item_name(small_box, 3) == "Маленький бокс — 3 ложечки"
    assert item_name(small_box, 1) == "Маленький бокс — 1 ложечка"
    assert item_name(small_box, 5) == "Маленький бокс — 5 ложечек"


def test_production_days_takes_longest_box():
    small = make_product(production_days=1)
    big = make_product(name="Большой бокс", production_days=3)

    assert compute_production_days([(small, [])]) == 1
    assert compute_production_days([(small, []), (big, [])]) == 3


def test_video_days_are_counted_for_each_box():
    small = make_product(production_days=1)
    video = make_addon()

    # Один бокс с видео — плюс день.
    assert compute_production_days([(small, [video])]) == 2
    # Видео на двух боксах из трёх — плюс два дня.
    assert compute_production_days([(small, [video]), (small, [video]), (small, [])]) == 3


def test_order_level_addon_counted_once():
    small = make_product(production_days=1)
    wrapping = make_addon(code="wrap", extra_production_days=1)

    assert compute_production_days([(small, []), (small, [])], [wrapping]) == 2
