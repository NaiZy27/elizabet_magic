"""Общие фикстуры.

Модели SQLAlchemy можно собирать в памяти без базы — значения по умолчанию из
mapped_column применяются только при вставке, поэтому фабрики заполняют поля явно.
"""

from __future__ import annotations

import datetime as dt
from itertools import count

import pytest

from core.enums import AddonChargeMode
from core.models import Addon, Product, ProductVariant
from core.workdays import WorkingCalendar

_ids = count(1)


def make_product(
    *,
    name: str = "Маленький бокс",
    production_days: int = 1,
    extra_spoon_price_kopecks: int | None = None,
    max_spoon_count: int | None = None,
    is_active: bool = True,
) -> Product:
    product = Product(
        id=next(_ids),
        sku=f"box-{next(_ids)}",
        name=name,
        production_days=production_days,
        extra_spoon_price_kopecks=extra_spoon_price_kopecks,
        max_spoon_count=max_spoon_count,
        is_active=is_active,
        sort_order=0,
    )
    product.variants = []
    return product


def add_variant(
    product: Product,
    *,
    spoon_count: int,
    price_kopecks: int,
    is_active: bool = True,
    sort_order: int = 0,
) -> ProductVariant:
    variant = ProductVariant(
        id=next(_ids),
        product_id=product.id,
        name=f"{spoon_count} ложечки",
        sku=f"var-{next(_ids)}",
        spoon_count=spoon_count,
        price_kopecks=price_kopecks,
        is_active=is_active,
        sort_order=sort_order,
    )
    variant.product = product
    product.variants.append(variant)
    return variant


def make_addon(
    *,
    code: str = "video",
    name: str = "Видео сборки",
    price_kopecks: int = 30000,
    charge_mode: AddonChargeMode = AddonChargeMode.PER_BOX,
    extra_production_days: int = 1,
    is_active: bool = True,
) -> Addon:
    return Addon(
        id=next(_ids),
        code=code,
        name=name,
        price_kopecks=price_kopecks,
        charge_mode=charge_mode,
        extra_production_days=extra_production_days,
        is_active=is_active,
        sort_order=0,
    )


@pytest.fixture
def small_box() -> Product:
    """Бокс из ТЗ: 1 ложечка — 500 ₽, 2 — 900 ₽, 3 — 1200 ₽, дальше по 300 ₽."""
    product = make_product(extra_spoon_price_kopecks=30000, max_spoon_count=6)
    add_variant(product, spoon_count=1, price_kopecks=50000, sort_order=1)
    add_variant(product, spoon_count=2, price_kopecks=90000, sort_order=2)
    add_variant(product, spoon_count=3, price_kopecks=120000, sort_order=3)
    return product


@pytest.fixture
def workdays() -> WorkingCalendar:
    """Рабочие дни — будни, без выходных дат."""
    return WorkingCalendar()


@pytest.fixture
def monday() -> dt.date:
    """21.09.2026 — понедельник."""
    return dt.date(2026, 9, 21)
