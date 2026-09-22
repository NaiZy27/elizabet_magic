"""Доставка: поиск пунктов выдачи и стоимость.

Пункты берутся из кэша `pickup_points`, который обновляется фоновой задачей: бот не
ходит в API службы, пока клиент листает список. Стоимость считается через провайдера,
а если API недоступно или служба ещё не подключена — по резервной цене из настроек.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.enums import DeliveryProviderCode, ProviderEnvironment
from core.errors import NotFoundError
from core.models import DeliveryProvider, Order, PickupPoint
from core.text import search_words

#: Сколько пунктов показываем клиенту за раз.
PICKUP_POINTS_PAGE_SIZE = 5

#: В каком радиусе ищем ближайшие пункты по геопозиции.
NEAREST_SEARCH_RADIUS_KM = 50.0

_EARTH_RADIUS_KM = 6371.0


@dataclass(frozen=True, slots=True)
class Parcel:
    """Посылка целиком: суммарный вес и габариты самой большой коробки."""

    weight_g: int
    length_cm: int
    width_cm: int
    height_cm: int


@dataclass(frozen=True, slots=True)
class DeliveryQuote:
    """Стоимость доставки до выбранного пункта."""

    price_kopecks: int
    #: True, если цена взята из настроек, а не посчитана службой.
    is_fallback: bool
    provider_code: DeliveryProviderCode
    provider_name: str
    days_min: int | None = None
    days_max: int | None = None


#: Габариты по умолчанию, пока владелица не измерила упаковку (открытый вопрос ТЗ).
DEFAULT_PARCEL = Parcel(weight_g=500, length_cm=20, width_cm=15, height_cm=10)


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Расстояние по прямой между двумя точками."""
    lat1_rad, lat2_rad = math.radians(lat1), math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def parcel_for_order(order: Order) -> Parcel:
    """Во что упакован заказ.

    Вес складываем по всем боксам, габариты берём по самому большому — коробки
    едут одной посылкой. Если размеры ещё не заведены, используем умолчание.
    """
    weight = 0
    length = width = height = 0
    known = False
    for item in order.items:
        variant = item.variant
        if variant is None or not variant.has_parcel_dimensions:
            continue
        known = True
        weight += (variant.weight_g or 0) * item.quantity
        length = max(length, variant.length_cm or 0)
        width = max(width, variant.width_cm or 0)
        height = max(height, variant.height_cm or 0)

    if not known or weight <= 0:
        return DEFAULT_PARCEL
    return Parcel(weight_g=weight, length_cm=length, width_cm=width, height_cm=height)


async def enabled_providers(session: AsyncSession) -> list[DeliveryProvider]:
    """Службы, включённые в админке."""
    result = await session.scalars(
        select(DeliveryProvider)
        .where(DeliveryProvider.is_enabled.is_(True))
        .order_by(DeliveryProvider.id),
    )
    return list(result)


async def get_provider(
    session: AsyncSession,
    code: DeliveryProviderCode,
) -> DeliveryProvider:
    provider = await session.scalar(
        select(DeliveryProvider).where(DeliveryProvider.code == code),
    )
    if provider is None:
        raise NotFoundError("Служба доставки не настроена")
    return provider


@dataclass(frozen=True, slots=True)
class TextSearchResult:
    points: list[PickupPoint]
    #: False — точного совпадения по адресу нет, показаны пункты по первому слову
    #: запроса (обычно это город).
    exact: bool
    #: Слово, по которому искали, когда совпадение неточное.
    fallback_word: str | None = None


async def search_by_text(
    session: AsyncSession,
    query: str,
    *,
    environment: ProviderEnvironment,
    limit: int = PICKUP_POINTS_PAGE_SIZE,
    offset: int = 0,
    providers: Sequence[DeliveryProviderCode] | None = None,
) -> TextSearchResult:
    """Пункты выдачи по городу и адресу, как их пишут люди.

    «москва, ул березовая аллея 19к1» разбирается на слова, служебные слова вроде
    «ул» и «д» отбрасываются, и пункт должен содержать каждое оставшееся слово.
    Если так ничего не нашлось, ищем по первому слову — обычно это город, и клиент
    увидит хотя бы пункты в своём городе.
    """
    words = search_words(query)
    if not words:
        return TextSearchResult(points=[], exact=True)

    points = await _search_words(session, words, environment, limit, offset, providers)
    if points or len(words) == 1:
        return TextSearchResult(points=points, exact=True)

    fallback = await _search_words(session, words[:1], environment, limit, offset, providers)
    return TextSearchResult(points=fallback, exact=False, fallback_word=words[0])


async def _search_words(
    session: AsyncSession,
    words: Sequence[str],
    environment: ProviderEnvironment,
    limit: int,
    offset: int,
    providers: Sequence[DeliveryProviderCode] | None,
) -> list[PickupPoint]:
    statement = (
        select(PickupPoint)
        .where(
            PickupPoint.is_active.is_(True),
            PickupPoint.environment == environment,
            *(PickupPoint.search_text.contains(word, autoescape=True) for word in words),
        )
        .order_by(PickupPoint.city, PickupPoint.address)
        .offset(offset)
        .limit(limit)
    )
    if providers:
        statement = statement.where(PickupPoint.provider.in_(tuple(providers)))
    result = await session.scalars(statement)
    return list(result)


async def search_nearest(
    session: AsyncSession,
    *,
    latitude: float,
    longitude: float,
    environment: ProviderEnvironment,
    limit: int = PICKUP_POINTS_PAGE_SIZE,
    radius_km: float = NEAREST_SEARCH_RADIUS_KM,
    providers: Sequence[DeliveryProviderCode] | None = None,
) -> list[tuple[PickupPoint, float]]:
    """Ближайшие пункты с расстоянием до каждого.

    Сначала отсекаем прямоугольником по координатам (по нему есть индекс),
    точное расстояние считаем уже по отобранным точкам.
    """
    lat_delta = radius_km / 111.0
    # Чем ближе к полюсу, тем короче градус долготы.
    lon_delta = radius_km / max(1.0, 111.0 * math.cos(math.radians(latitude)))

    statement = select(PickupPoint).where(
        PickupPoint.is_active.is_(True),
        PickupPoint.environment == environment,
        PickupPoint.latitude.between(latitude - lat_delta, latitude + lat_delta),
        PickupPoint.longitude.between(longitude - lon_delta, longitude + lon_delta),
    )
    if providers:
        statement = statement.where(PickupPoint.provider.in_(tuple(providers)))

    result = await session.scalars(statement)
    with_distance = [
        (point, distance_km(latitude, longitude, point.latitude, point.longitude))
        for point in result
    ]
    with_distance.sort(key=lambda pair: pair[1])
    return [pair for pair in with_distance if pair[1] <= radius_km][:limit]


async def get_pickup_point(session: AsyncSession, point_id: int) -> PickupPoint:
    point = await session.get(PickupPoint, point_id)
    if point is None or not point.is_active:
        raise NotFoundError("Этот пункт выдачи больше недоступен, выберите другой")
    return point


async def quote(
    session: AsyncSession,
    *,
    point: PickupPoint,
    parcel: Parcel,
) -> DeliveryQuote:
    """Стоимость доставки до пункта выдачи.

    Клиент API службы подключается здесь же: пока его нет, цена берётся резервная
    из настроек службы, и заказ помечается событием об этом.
    """
    provider = await get_provider(session, DeliveryProviderCode(point.provider))
    # Здесь появится вызов API службы, когда будет заключён договор.
    return DeliveryQuote(
        price_kopecks=provider.fallback_price_kopecks,
        is_fallback=True,
        provider_code=DeliveryProviderCode(point.provider),
        provider_name=provider.name,
    )


def fits(point: PickupPoint, parcel: Parcel) -> bool:
    """Примет ли пункт выдачи такую посылку."""
    if point.max_weight_g is not None and parcel.weight_g > point.max_weight_g:
        return False
    return True
