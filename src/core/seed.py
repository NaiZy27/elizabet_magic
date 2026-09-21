"""Стартовое наполнение базы.

Запуск: python -m core.seed

Команда идемпотентна: повторный запуск ничего не дублирует и не затирает то,
что владелица уже поправила в админке.
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import session_scope
from core.enums import (
    VIDEO_ADDON_CODE,
    AddonChargeMode,
    DeliveryProviderCode,
    ProviderEnvironment,
)
from core.models import (
    Addon,
    Color,
    DeliveryProvider,
    MessageTemplate,
    PickupPoint,
    Product,
    ProductVariant,
)
from core.services.notifications import DEFAULT_TEMPLATES

#: Палитра из ТЗ.
COLORS = [
    "Розовый",
    "Голубой",
    "Сиреневый",
    "Фиолетовый",
    "Белый",
    "Чёрный",
    "Жёлтый",
    "Зелёный",
]

#: Маленький бокс с ценами из ТЗ: 1 ложечка — 500 ₽, 2 — 900 ₽, 3 — 1200 ₽,
#: каждая следующая — по 300 ₽, максимум 6.
SMALL_BOX_VARIANTS = [
    ("1 ложечка", "box-small-1", 1, 50000),
    ("2 ложечки", "box-small-2", 2, 90000),
    ("3 ложечки", "box-small-3", 3, 120000),
]


async def seed_colors(session: AsyncSession) -> int:
    existing = set(await session.scalars(select(Color.name)))
    added = 0
    for position, name in enumerate(COLORS, start=1):
        if name in existing:
            continue
        session.add(Color(name=name, sort_order=position, is_active=True))
        added += 1
    await session.flush()
    return added


async def seed_addons(session: AsyncSession) -> int:
    existing = set(await session.scalars(select(Addon.code)))
    if VIDEO_ADDON_CODE in existing:
        return 0
    session.add(
        Addon(
            code=VIDEO_ADDON_CODE,
            name="Видео сборки",
            description="Снимем, как собираем именно ваш бокс, и пришлём видео.",
            price_kopecks=30000,
            charge_mode=AddonChargeMode.PER_BOX,
            extra_production_days=1,
            sort_order=1,
            is_active=True,
        ),
    )
    await session.flush()
    return 1


async def seed_products(session: AsyncSession) -> int:
    existing = set(await session.scalars(select(Product.sku)))
    if "box-small" in existing:
        return 0

    product = Product(
        sku="box-small",
        name="Маленький бокс",
        description="Компактный подарочный бокс с ложечками.",
        production_days=1,
        extra_spoon_price_kopecks=30000,
        max_spoon_count=6,
        sort_order=1,
        is_active=True,
    )
    session.add(product)
    await session.flush()

    for position, (name, sku, spoons, price) in enumerate(SMALL_BOX_VARIANTS, start=1):
        session.add(
            ProductVariant(
                product_id=product.id,
                name=name,
                sku=sku,
                spoon_count=spoons,
                price_kopecks=price,
                sort_order=position,
                is_active=True,
            ),
        )
    await session.flush()
    return 1


async def seed_delivery_providers(session: AsyncSession) -> int:
    existing = set(await session.scalars(select(DeliveryProvider.code)))
    added = 0
    # Обе службы заведены выключенными: включаем ту, с которой заключён договор.
    for code, name in (
        (DeliveryProviderCode.CDEK, "СДЭК"),
        (DeliveryProviderCode.YANDEX, "Яндекс Доставка"),
    ):
        if code in existing:
            continue
        session.add(
            DeliveryProvider(
                code=code,
                name=name,
                is_enabled=False,
                fallback_price_kopecks=30000,
            ),
        )
        added += 1
    await session.flush()
    return added


async def seed_message_templates(session: AsyncSession) -> int:
    existing = set(await session.scalars(select(MessageTemplate.key)))
    added = 0
    for key, text in DEFAULT_TEMPLATES.items():
        if key.value in existing:
            continue
        session.add(MessageTemplate(key=key.value, text=text, is_enabled=True))
        added += 1
    await session.flush()
    return added


#: Демонстрационные пункты выдачи для тестового контура.
#: Нужны, чтобы пройти оформление целиком, пока не подключена служба доставки.
DEMO_PICKUP_POINTS = [
    ("demo-msk-1", "Пункт выдачи на Тверской", "Москва", "г. Москва, ул. Тверская, 15"),
    ("demo-msk-2", "Пункт выдачи на Арбате", "Москва", "г. Москва, ул. Арбат, 24"),
    ("demo-spb-1", "Пункт выдачи на Невском", "Санкт-Петербург", "г. Санкт-Петербург, Невский пр., 60"),
    ("demo-kzn-1", "Пункт выдачи на Баумана", "Казань", "г. Казань, ул. Баумана, 38"),
]

DEMO_COORDINATES = {
    "demo-msk-1": (55.7649, 37.6055),
    "demo-msk-2": (55.7494, 37.5911),
    "demo-spb-1": (59.9329, 30.3509),
    "demo-kzn-1": (55.7903, 49.1221),
}


async def seed_demo_pickup_points(session: AsyncSession) -> int:
    """Завести тестовые ПВЗ. В боевом контуре эти точки не появятся."""
    existing = set(
        await session.scalars(
            select(PickupPoint.external_id).where(
                PickupPoint.environment == ProviderEnvironment.TEST,
            ),
        ),
    )
    added = 0
    for external_id, name, city, address in DEMO_PICKUP_POINTS:
        if external_id in existing:
            continue
        latitude, longitude = DEMO_COORDINATES[external_id]
        session.add(
            PickupPoint(
                provider=DeliveryProviderCode.CDEK,
                environment=ProviderEnvironment.TEST,
                external_id=external_id,
                name=name,
                city=city,
                address=address,
                latitude=latitude,
                longitude=longitude,
                working_hours="Пн-Пт 10:00–20:00, Сб 10:00–18:00",
                is_active=True,
            ),
        )
        added += 1
    await session.flush()
    return added


async def seed_all(*, with_demo_points: bool = False) -> dict[str, int]:
    async with session_scope() as session:
        created = {
            "цвета": await seed_colors(session),
            "услуги": await seed_addons(session),
            "боксы": await seed_products(session),
            "службы доставки": await seed_delivery_providers(session),
            "шаблоны сообщений": await seed_message_templates(session),
        }
        if with_demo_points:
            created["тестовые пункты выдачи"] = await seed_demo_pickup_points(session)
        return created


def main() -> None:
    parser = argparse.ArgumentParser(description="Наполнить базу стартовыми данными")
    parser.add_argument(
        "--demo-pickup-points",
        action="store_true",
        help="добавить тестовые пункты выдачи, чтобы пройти оформление без службы доставки",
    )
    args = parser.parse_args()

    created = asyncio.run(seed_all(with_demo_points=args.demo_pickup_points))
    for name, count in created.items():
        print(f"{name}: добавлено {count}")  # noqa: T201 — это консольная команда
    print(  # noqa: T201 — это консольная команда
        "\nГотово. Средние и большие боксы заведите в админке: "
        "их цены и габариты в ТЗ не зафиксированы.",
    )


if __name__ == "__main__":
    main()
