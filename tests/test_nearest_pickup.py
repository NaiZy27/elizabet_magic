"""Поиск ближайших пунктов выдачи: расширение радиуса и порядок по расстоянию.

База не нужна: подменная сессия отдаёт все точки на любой запрос, а отсечение
по кругу и сортировка проверяются в самом поиске.
"""

from __future__ import annotations

import asyncio
from itertools import count

from core.enums import DeliveryProviderCode, ProviderEnvironment
from core.models import PickupPoint
from core.services.delivery import distance_km, search_nearest

_ids = count(1)

# Красная площадь
HOME = (55.7539, 37.6208)


def make_point(lat: float, lon: float) -> PickupPoint:
    return PickupPoint(
        id=next(_ids),
        provider=DeliveryProviderCode.CDEK,
        environment=ProviderEnvironment.PRODUCTION,
        external_id=f"p{next(_ids)}",
        name="ПВЗ",
        city="Москва",
        address=f"{lat}, {lon}",
        latitude=lat,
        longitude=lon,
        is_active=True,
    )


class FakeSession:
    """Отдаёт все точки, считает запросы — по ним видно, сколько радиусов перебрано."""

    def __init__(self, points: list[PickupPoint]) -> None:
        self.points = points
        self.queries = 0

    async def scalars(self, _statement):
        self.queries += 1
        return list(self.points)


def run(session: FakeSession, *, limit: int = 5, radii=(2.0, 5.0, 15.0)):
    return asyncio.run(
        search_nearest(
            session,
            latitude=HOME[0],
            longitude=HOME[1],
            environment=ProviderEnvironment.PRODUCTION,
            limit=limit,
            radii_km=radii,
            # Службы явно: иначе поиск сначала спросит у базы список включённых.
            providers=[DeliveryProviderCode.CDEK],
        ),
    )


def test_returns_closest_sorted_by_distance():
    far, near, middle = (
        make_point(55.80, 37.62),
        make_point(55.754, 37.621),
        make_point(55.76, 37.63),
    )
    found = run(FakeSession([far, near, middle]), limit=3)
    assert [point for point, _ in found] == [near, middle, far]
    assert [km for _, km in found] == sorted(km for _, km in found)


def test_stops_at_first_radius_with_enough_points():
    points = [make_point(HOME[0] + 0.001 * i, HOME[1]) for i in range(1, 7)]  # все в пределах 1 км
    session = FakeSession(points)
    found = run(session, limit=5)
    assert len(found) == 5
    assert session.queries == 1


def test_expands_radius_until_limit_reached():
    # 2 точки рядом, остальные — в 10 и 1000 км: первые два радиуса их не набирают
    points = [
        make_point(HOME[0] + 0.001, HOME[1]),
        make_point(HOME[0] + 0.002, HOME[1]),
        make_point(HOME[0] + 0.09, HOME[1]),
        make_point(HOME[0] + 0.091, HOME[1]),
        make_point(HOME[0] + 9.0, HOME[1]),
    ]
    session = FakeSession(points)
    found = run(session, limit=5)
    assert len(found) == 5
    # 2 км → 5 км → 15 км (только 4 точки) → весь справочник
    assert session.queries == 4
    assert found[-1][1] > 900


def test_distance_reported_matches_haversine():
    point = make_point(55.7558, 37.6173)
    ((_, km),) = run(FakeSession([point]), limit=1)
    assert abs(km - distance_km(*HOME, 55.7558, 37.6173)) < 1e-9


def test_empty_directory_returns_nothing():
    assert run(FakeSession([])) == []


def test_no_enabled_providers_means_no_points():
    """Если в панели не включена ни одна служба, пунктов клиенту не показываем."""
    session = FakeSession([make_point(55.754, 37.621)])
    found = asyncio.run(
        search_nearest(
            session,
            latitude=HOME[0],
            longitude=HOME[1],
            environment=ProviderEnvironment.PRODUCTION,
            providers=[],
        ),
    )
    assert found == []
    assert session.queries == 0
