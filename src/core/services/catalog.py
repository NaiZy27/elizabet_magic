"""Каталог: что показываем в боте, сколько стоит заказ и сколько его собирать.

Итоговая сумма считается только здесь. Клиент присылает выбор (вариант, число
ложечек, услуги к каждому боксу), цену за него мы берём из базы — числам из
callback_data не доверяем.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.enums import VIDEO_ADDON_CODE, AddonChargeMode
from core.errors import NotFoundError, ValidationError
from core.models import Addon, Color, Product, ProductVariant
from core.text import spoons_phrase

# --- что просит клиент ---


@dataclass(frozen=True, slots=True)
class RequestedItem:
    """Выбор клиента по одному боксу: вариант, ложечки и услуги к этому боксу."""

    variant_id: int
    quantity: int = 1
    #: None — столько, сколько в самом варианте; больше — докупка ложечек.
    spoon_count: int | None = None
    #: Услуги к этому боксу — например, видео его сборки.
    addon_ids: tuple[int, ...] = ()


# --- что посчитали ---


@dataclass(frozen=True, slots=True)
class PricedAddon:
    addon: Addon
    quantity: int
    unit_price_kopecks: int
    name_snapshot: str

    @property
    def total_kopecks(self) -> int:
        return self.unit_price_kopecks * self.quantity


@dataclass(frozen=True, slots=True)
class PricedItem:
    variant: ProductVariant
    quantity: int
    spoon_count: int
    unit_price_kopecks: int
    name_snapshot: str
    #: Услуги «за каждый бокс», выбранные к этому боксу.
    addons: list[PricedAddon] = field(default_factory=list)

    @property
    def total_kopecks(self) -> int:
        return self.unit_price_kopecks * self.quantity + sum(
            addon.total_kopecks for addon in self.addons
        )


@dataclass(frozen=True, slots=True)
class PricedOrder:
    items: list[PricedItem] = field(default_factory=list)
    #: Услуги «один раз на заказ».
    addons: list[PricedAddon] = field(default_factory=list)
    delivery_kopecks: int = 0
    production_days: int = 1

    @property
    def subtotal_kopecks(self) -> int:
        return sum(item.total_kopecks for item in self.items) + sum(
            addon.total_kopecks for addon in self.addons
        )

    @property
    def total_kopecks(self) -> int:
        return self.subtotal_kopecks + self.delivery_kopecks

    @property
    def box_count(self) -> int:
        return sum(item.quantity for item in self.items)


# --- чистые правила ---


def active_variants(variants: Sequence[ProductVariant]) -> list[ProductVariant]:
    return sorted(
        (variant for variant in variants if variant.is_active),
        key=lambda variant: (variant.sort_order, variant.spoon_count),
    )


def largest_variant(variants: Sequence[ProductVariant]) -> ProductVariant | None:
    """Самый большой активный вариант — от него считается докупка ложечек."""
    active = active_variants(variants)
    if not active:
        return None
    return max(active, key=lambda variant: variant.spoon_count)


def spoon_options(product: Product, variants: Sequence[ProductVariant]) -> list[int]:
    """Все количества ложечек, которые можно выбрать: из вариантов плюс докупка."""
    counts = {variant.spoon_count for variant in active_variants(variants)}
    biggest = largest_variant(variants)
    if product.allows_extra_spoons and biggest is not None and product.max_spoon_count is not None:
        counts.update(range(biggest.spoon_count + 1, product.max_spoon_count + 1))
    return sorted(counts)


def price_for_spoons(
    product: Product,
    variants: Sequence[ProductVariant],
    spoon_count: int,
) -> tuple[ProductVariant, int]:
    """Вариант-основа и цена бокса с заданным числом ложечек.

    Точное совпадение берём из варианта. Всё, что больше самого большого варианта, —
    докупка: цена большого варианта плюс каждая ложечка сверх него.
    """
    active = active_variants(variants)
    if not active:
        raise ValidationError("У этого бокса сейчас нет доступных вариантов")

    for variant in active:
        if variant.spoon_count == spoon_count:
            return variant, variant.price_kopecks

    biggest = largest_variant(variants)
    if (
        biggest is not None
        and product.allows_extra_spoons
        and product.max_spoon_count is not None
        and product.extra_spoon_price_kopecks is not None
        and biggest.spoon_count < spoon_count <= product.max_spoon_count
    ):
        extra_spoons = spoon_count - biggest.spoon_count
        price = biggest.price_kopecks + extra_spoons * product.extra_spoon_price_kopecks
        return biggest, price

    raise ValidationError(f"Бокс на {spoons_phrase(spoon_count)} заказать нельзя")


def item_name(product: Product, spoon_count: int) -> str:
    """Название позиции для снимка и для карточки: «Маленький бокс — 3 ложечки»."""
    return f"{product.name} — {spoons_phrase(spoon_count)}"


def compute_production_days(
    boxes: Sequence[tuple[Product, Sequence[Addon]]],
    order_addons: Sequence[Addon] = (),
) -> int:
    """Срок изготовления заказа в рабочих днях.

    `boxes` — по записи на каждый физический бокс: сам бокс и услуги к нему.
    База — самый долгий бокс. Услуги к боксу удлиняют срок за каждый бокс
    отдельно: видео на двух боксах — плюс два дня. Услуги на весь заказ — один раз.
    Предположение «несколько боксов без услуг собираются не дольше одного» — открытый
    вопрос ТЗ, при необходимости правится здесь.
    """
    base = max((product.production_days for product, _ in boxes), default=1)
    per_box = sum(addon.extra_production_days for _, addons in boxes for addon in addons)
    per_order = sum(addon.extra_production_days for addon in order_addons)
    return max(1, base + per_box + per_order)


# --- работа с базой ---


async def list_active_products(session: AsyncSession) -> list[Product]:
    """Боксы для бота: включённые и имеющие хотя бы один активный вариант."""
    result = await session.scalars(
        select(Product)
        .options(selectinload(Product.variants))
        .where(Product.is_active.is_(True))
        .order_by(Product.sort_order, Product.id),
    )
    return [product for product in result if active_variants(product.variants)]


async def list_products_for_admin(session: AsyncSession) -> list[Product]:
    """Все боксы, включая выключенные, — в админке видно всё."""
    result = await session.scalars(
        select(Product)
        .options(selectinload(Product.variants))
        .order_by(Product.sort_order, Product.id),
    )
    return list(result)


async def get_product(session: AsyncSession, product_id: int) -> Product:
    product = await session.scalar(
        select(Product).options(selectinload(Product.variants)).where(Product.id == product_id),
    )
    if product is None:
        raise NotFoundError("Бокс не найден")
    return product


async def list_active_colors(session: AsyncSession) -> list[Color]:
    result = await session.scalars(
        select(Color).where(Color.is_active.is_(True)).order_by(Color.sort_order, Color.id),
    )
    return list(result)


async def color_names(session: AsyncSession, color_ids: Sequence[int]) -> list[str]:
    """Названия цветов в порядке палитры — для снимка пожеланий в заказе."""
    if not color_ids:
        return []
    result = await session.scalars(
        select(Color).where(Color.id.in_(set(color_ids))).order_by(Color.sort_order, Color.id),
    )
    return [color.name for color in result]


async def color_ids_by_names(session: AsyncSession, names: Sequence[str]) -> list[int]:
    """Обратно: id активных цветов по названиям — чтобы отметить их при правке бокса."""
    if not names:
        return []
    result = await session.scalars(
        select(Color.id).where(Color.name.in_(set(names)), Color.is_active.is_(True)),
    )
    return list(result)


async def list_active_addons(session: AsyncSession) -> list[Addon]:
    result = await session.scalars(
        select(Addon).where(Addon.is_active.is_(True)).order_by(Addon.sort_order, Addon.id),
    )
    return list(result)


async def get_video_addon(session: AsyncSession) -> Addon | None:
    """Услуга «видео сборки», если она включена."""
    return await session.scalar(
        select(Addon).where(Addon.code == VIDEO_ADDON_CODE, Addon.is_active.is_(True)),
    )


async def price_order(
    session: AsyncSession,
    *,
    items: Sequence[RequestedItem],
    order_addon_ids: Sequence[int] = (),
    delivery_kopecks: int = 0,
) -> PricedOrder:
    """Посчитать заказ целиком: боксы с их услугами, общие услуги, срок изготовления.

    Единственное место, где рождается итоговая сумма. Если к боксу прислали услугу
    «один раз на заказ», она переезжает на заказ: брать её за каждый бокс нельзя.
    """
    if not items:
        raise ValidationError("В заказе нет ни одного бокса")
    if delivery_kopecks < 0:
        raise ValidationError("Стоимость доставки не может быть отрицательной")

    variants = await _load_variants(session, [item.variant_id for item in items])
    all_addon_ids = {addon_id for item in items for addon_id in item.addon_ids}
    all_addon_ids.update(order_addon_ids)
    addons = await _load_addons(session, all_addon_ids)

    order_level: set[int] = set(order_addon_ids)
    priced_items: list[PricedItem] = []
    boxes: list[tuple[Product, list[Addon]]] = []
    for item in items:
        if item.quantity <= 0:
            raise ValidationError("Количество боксов должно быть больше нуля")
        variant = variants[item.variant_id]
        product = variant.product
        spoon_count = item.spoon_count if item.spoon_count is not None else variant.spoon_count
        base_variant, unit_price = price_for_spoons(product, product.variants, spoon_count)

        box_addons: list[Addon] = []
        for addon_id in dict.fromkeys(item.addon_ids):
            addon = addons[addon_id]
            if addon.charge_mode == AddonChargeMode.PER_ORDER:
                order_level.add(addon_id)
            else:
                box_addons.append(addon)

        priced_items.append(
            PricedItem(
                variant=base_variant,
                quantity=item.quantity,
                spoon_count=spoon_count,
                unit_price_kopecks=unit_price,
                name_snapshot=item_name(product, spoon_count),
                addons=[_price_addon(addon, quantity=item.quantity) for addon in box_addons],
            ),
        )
        boxes.extend((product, box_addons) for _ in range(item.quantity))

    order_addons = sorted(
        (addons[addon_id] for addon_id in order_level),
        key=lambda addon: (addon.sort_order, addon.id),
    )
    return PricedOrder(
        items=priced_items,
        addons=[_price_addon(addon, quantity=1) for addon in order_addons],
        delivery_kopecks=delivery_kopecks,
        production_days=compute_production_days(boxes, order_addons),
    )


async def _load_variants(
    session: AsyncSession,
    variant_ids: Sequence[int],
) -> dict[int, ProductVariant]:
    """Варианты вместе с их боксом и остальными вариантами — для расчёта докупки."""
    unique_ids = set(variant_ids)
    result = await session.scalars(
        select(ProductVariant)
        .options(selectinload(ProductVariant.product).selectinload(Product.variants))
        .where(ProductVariant.id.in_(unique_ids)),
    )
    variants = {variant.id: variant for variant in result}
    missing = unique_ids - variants.keys()
    if missing:
        raise NotFoundError("Такого бокса больше нет в каталоге")
    for variant in variants.values():
        if not variant.is_active or not variant.product.is_active:
            raise ValidationError(f"«{variant.product.name}» сейчас недоступен для заказа")
    return variants


async def _load_addons(session: AsyncSession, addon_ids: set[int]) -> dict[int, Addon]:
    if not addon_ids:
        return {}
    result = await session.scalars(select(Addon).where(Addon.id.in_(addon_ids)))
    addons = {addon.id: addon for addon in result}
    if addons.keys() != addon_ids:
        raise NotFoundError("Дополнительная услуга не найдена")
    for addon in addons.values():
        if not addon.is_active:
            raise ValidationError(f"Услуга «{addon.name}» сейчас недоступна")
    return addons


def _price_addon(addon: Addon, *, quantity: int) -> PricedAddon:
    return PricedAddon(
        addon=addon,
        quantity=quantity,
        unit_price_kopecks=addon.price_kopecks,
        name_snapshot=addon.name,
    )
