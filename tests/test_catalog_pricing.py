"""Цена ложечек: варианты из каталога и каждая следующая сверх самого большого."""

from __future__ import annotations

import pytest

from core.errors import ValidationError
from core.services.catalog import (
    compute_production_days,
    item_name,
    largest_variant,
    price_for_spoons,
    spoon_options,
    spoon_prices_text,
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
    small_box.name = "Маленькие ложечки"
    assert item_name(small_box, 3) == "Маленькие ложечки — 3 шт."
    assert item_name(small_box, 1) == "Маленькие ложечки — 1 шт."


def test_spoon_prices_read_as_a_ladder(small_box):
    """Клиенту: «1 ложечка — 500 ₽, 2-я — 400 ₽, каждая следующая — 300 ₽»."""
    assert spoon_prices_text(small_box) == (
        "1 ложечка — 500\xa0₽, 2-я — 400\xa0₽, каждая следующая — 300\xa0₽"
    )


def test_big_spoon_prices():
    """Большие: 1000, вторая 900, дальше по 800."""
    big = make_product(name="Большие ложечки", extra_spoon_price_kopecks=80000, max_spoon_count=6)
    add_variant(big, spoon_count=1, price_kopecks=100000, sort_order=1)
    add_variant(big, spoon_count=2, price_kopecks=190000, sort_order=2)

    assert price_for_spoons(big, big.variants, 4)[1] == 350000  # 1000 + 900 + 800 + 800
    assert spoon_prices_text(big) == (
        "1 ложечка — 1\xa0000\xa0₽, 2-я — 900\xa0₽, каждая следующая — 800\xa0₽"
    )


def test_production_days_takes_longest_box():
    small = make_product(production_days=1)
    big = make_product(name="Большой бокс", production_days=3)

    assert compute_production_days([(small, [])]) == 1
    assert compute_production_days([(small, []), (big, [])]) == 3


def test_video_order_takes_two_days_however_many_positions():
    small = make_product(production_days=1)
    video = make_addon()

    # С видео — 2 дня, без — 1; число позиций срок не растягивает (это дело лимита дня).
    assert compute_production_days([(small, [video])]) == 2
    assert compute_production_days([(small, [video]), (small, [video]), (small, [])]) == 2
    assert compute_production_days([(small, []), (small, [])]) == 1


def test_order_level_addon_counted_once():
    small = make_product(production_days=1)
    wrapping = make_addon(code="wrap", extra_production_days=1)

    assert compute_production_days([(small, []), (small, [])], [wrapping]) == 2
