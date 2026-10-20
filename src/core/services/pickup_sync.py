"""Автообновление пунктов выдачи из публичных данных служб, без ключей.

- СДЭК — публичный список офисов (`integration.cdek.ru/pvzlist/v1/json`, остался от
  первой версии их API): только собственные офисы СДЭК в России, ~4,7 тыс.;
- Яндекс Доставка — тот же адрес, что у виджета выбора ПВЗ Яндекса для сайтов
  (`widget-pvz.dostavka.yandex.net`): пункты Яндекс Маркета и 5Post, с их id, часами
  работы и датой закрытия.

- Ozon — OpenStreetMap через Overpass API: пункты с brand=Ozon, адрес — у дома, в
  котором стоит пункт. Данные волонтёров: часть пунктов Ozon в OSM нет, но Москва и
  крупные города покрыты хорошо. Сайт и карта самого Ozon без ключей не отдаются.

Обновление раз в неделю (worker.tasks.pickup) и вручную:

    python -m core.services.pickup_sync cdek yandex ozon

Память берегём: Яндекс запрашивается полосами по широте, в базу пишем пачками через
INSERT … ON CONFLICT. Пункты, которых не стало в
источнике, выключаются (не удаляются — на них ссылаются заказы). Если источник
вдруг отдал подозрительно мало пунктов, ничего не выключаем: вероятнее, сломался
источник, чем закрылась половина сети.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import math
import re
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.clock import now_utc, today_moscow
from core.config import get_settings
from core.db import dispose_engine, session_scope
from core.enums import DeliveryProviderCode, ProviderEnvironment
from core.errors import ExternalServiceError
from core.models import DeliveryProvider, PickupPoint
from core.text import normalize_search

logger = logging.getLogger(__name__)

YANDEX_POINTS_URL = "https://widget-pvz.dostavka.yandex.net"
CDEK_POINTS_URL = "https://integration.cdek.ru/pvzlist/v1/json"

#: Сколько строк писать в базу за один INSERT.
BATCH_SIZE = 1000
#: Источник отдал меньше этой доли от уже известных пунктов — считаем, что он
#: сломался, и ничего не выключаем.
MIN_SHARE_OF_KNOWN = 0.5
#: Пункт Яндекса, который закрывается раньше, не предлагаем: посылка может не успеть.
YANDEX_CLOSING_MARGIN = dt.timedelta(days=14)

_TIMEOUT = httpx.Timeout(120.0, connect=20.0)
_WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


@dataclass(frozen=True, slots=True)
class PointRow:
    external_id: str
    name: str
    city: str
    address: str
    latitude: float
    longitude: float
    working_hours: str | None = None
    max_weight_g: int | None = None


# --- часы работы ---


def format_week(hours: dict[int, str | None]) -> str | None:
    """{0: "10:00–21:00", …, 6: None} -> «Пн–Пт 10:00–21:00, Сб 10:00–18:00».

    Дни недели 0 = понедельник. Подряд идущие дни с одинаковыми часами склеиваются.
    """
    if not any(hours.get(day) for day in range(7)):
        return None
    groups: list[tuple[int, int, str]] = []
    for day in range(7):
        value = hours.get(day)
        if value is None:
            continue
        if groups and groups[-1][1] == day - 1 and groups[-1][2] == value:
            groups[-1] = (groups[-1][0], day, value)
        else:
            groups.append((day, day, value))
    parts = []
    for first, last, value in groups:
        days = _WEEKDAYS[first] if first == last else f"{_WEEKDAYS[first]}–{_WEEKDAYS[last]}"
        parts.append(f"{days} {value}")
    return ", ".join(parts)[:256]


def _span(start: str, end: str) -> str:
    if start in {"00:00", "0:00"} and end in {"23:59", "24:00", "00:00"}:
        return "круглосуточно"
    return f"{start}–{end}"


# --- Яндекс Доставка ---


def yandex_city(locality: str) -> str:
    """«Москва г» -> «Москва»."""
    return re.sub(r"\s+г\.?$", "", locality.strip())


def yandex_hours(schedule: dict[str, Any] | None) -> str | None:
    hours: dict[int, str | None] = {}
    for restriction in (schedule or {}).get("restrictions") or []:
        start = restriction.get("time_from") or {}
        end = restriction.get("time_to") or {}
        span = _span(
            f"{int(start.get('hours', 0)):02d}:{int(start.get('minutes', 0)):02d}",
            f"{int(end.get('hours', 0)):02d}:{int(end.get('minutes', 0)):02d}",
        )
        for day in restriction.get("days") or []:
            if 1 <= int(day) <= 7:
                hours[int(day) - 1] = span
    return format_week(hours)


def yandex_row(point: dict[str, Any], *, today: dt.date) -> PointRow | None:
    """Пункт из виджета Яндекса -> строка справочника. None — пункт не берём."""
    address = point.get("address") or {}
    position = point.get("position") or {}
    if point.get("type") != "pickup_point" or address.get("country") != "Россия":
        # Постаматы не берём: бокс в ячейку может не войти.
        return None
    closing = point.get("deactivation_date")
    if closing:
        try:
            if dt.date.fromisoformat(str(closing)[:10]) <= today + YANDEX_CLOSING_MARGIN:
                return None
        except ValueError:
            pass
    if point.get("id") is None or position.get("latitude") is None:
        return None
    street_line = ", ".join(
        part
        for part in (
            address.get("street"),
            address.get("house"),
            f"корп. {address['housing']}" if address.get("housing") else None,
            f"стр. {address['building']}" if address.get("building") else None,
        )
        if part
    )
    return PointRow(
        external_id=str(point["id"]),
        name=str(point.get("name") or "Пункт выдачи Яндекса")[:256],
        city=yandex_city(str(address.get("locality") or ""))[:128],
        address=(street_line or str(address.get("full_address") or ""))[:512],
        latitude=float(position["latitude"]),
        longitude=float(position["longitude"]),
        working_hours=yandex_hours(point.get("schedule")),
    )


async def yandex_rows(failed: list[str]) -> AsyncIterator[list[PointRow]]:
    """Пункты Яндекса по России полосами в полградуса широты: ответ на всю страну
    сразу весит ~60 МБ, а полоса — единицы мегабайт (самая плотная — через Москву)."""
    today = today_moscow()
    seen: set[str] = set()
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for step in range(41 * 2, 82 * 2):
            body = {
                "latitude": {"from": step / 2, "to": (step + 1) / 2},
                "longitude": {"from": 19, "to": 180},
                "is_post_office": False,
            }
            try:
                response = await client.post(YANDEX_POINTS_URL, json=body)
                response.raise_for_status()
                points = response.json().get("points") or []
            except (httpx.HTTPError, ValueError) as error:
                raise ExternalServiceError(f"Яндекс не отдал пункты: {error}") from error
            batch = []
            for point in points:
                row = yandex_row(point, today=today)
                # Пункт на границе полос приходит дважды.
                if row is not None and row.external_id not in seen:
                    seen.add(row.external_id)
                    batch.append(row)
            if batch:
                yield batch


# --- СДЭК ---


def cdek_hours(point: dict[str, Any]) -> str | None:
    """workTimeYList: [{"day": "1", "periods": "10:00/20:00"}, …] -> «Пн–Пт 10:00–20:00, …»."""
    hours: dict[int, str | None] = {}
    for entry in point.get("workTimeYList") or []:
        try:
            day = int(entry.get("day"))
        except (TypeError, ValueError):
            continue
        start, _, end = str(entry.get("periods") or "").partition("/")
        if 1 <= day <= 7 and start and end:
            hours[day - 1] = _span(start, end)
    return format_week(hours) or (str(point["workTime"])[:256] if point.get("workTime") else None)


def cdek_row(point: dict[str, Any]) -> PointRow | None:
    """Пункт из публичного списка СДЭК -> строка справочника. None — пункт не берём.

    Берём только собственные офисы СДЭК в России, где выдают посылки. Постаматы и
    партнёрские пункты (Почта России и другие) — нет: в постамат бокс может не войти,
    а в партнёрский пункт владелица не всегда сможет отправить из приложения СДЭК.
    """
    if (
        point.get("countryCodeIso") != "RU"
        or point.get("type") != "PVZ"
        or point.get("ownerCode") != "CDEK"
        or not point.get("isHandout")
        or point.get("status", "ACTIVE") != "ACTIVE"
    ):
        return None
    try:
        latitude, longitude = float(point["coordY"]), float(point["coordX"])
    except (KeyError, TypeError, ValueError):
        return None
    if not point.get("code"):
        return None
    weight_max = (point.get("weightLimit") or {}).get("WeightMax")
    try:
        max_weight_g = int(float(weight_max) * 1000) if weight_max else None
    except ValueError:
        max_weight_g = None
    return PointRow(
        external_id=str(point["code"]),
        name="Пункт выдачи СДЭК",
        city=str(point.get("city") or "")[:128],
        address=str(point.get("address") or point.get("fullAddress") or "")[:512],
        latitude=latitude,
        longitude=longitude,
        working_hours=cdek_hours(point),
        max_weight_g=max_weight_g or None,
    )


async def cdek_rows(failed: list[str]) -> AsyncIterator[list[PointRow]]:
    """Публичный список пунктов СДЭК (без ключей) — весь сразу, ~18 МБ."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(CDEK_POINTS_URL)
            response.raise_for_status()
            points = response.json().get("pvz") or []
    except (httpx.HTTPError, ValueError) as error:
        raise ExternalServiceError(f"СДЭК не отдал пункты: {error}") from error
    rows = [row for row in map(cdek_row, points) if row is not None]
    del points
    for start in range(0, len(rows), BATCH_SIZE):
        yield rows[start : start + BATCH_SIZE]


# --- Ozon: OpenStreetMap ---

#: Публичные серверы Overpass API. Если первый перегружен — пробуем следующий.
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
#: Overpass просит представляться: так администраторы свяжутся, а не забанят.
_OVERPASS_HEADERS = {"User-Agent": "elizabet-magic/1.0 (weekly pickup points sync)"}
_OVERPASS_TIMEOUT = httpx.Timeout(900.0, connect=30.0)
#: Пауза между запросами по регионам: публичный Overpass бережём.
_OVERPASS_PAUSE_SECONDS = 5
#: Насколько далеко от пункта искать дом с адресом, если точка не внутри контура.
_HOUSE_RADIUS_M = 60
#: Насколько далеко искать населённый пункт, если город не указан в адресе.
_PLACE_RADIUS_M = 20_000
#: Города федерального значения: все пункты внутри — этот город (цена «по Москве»).
_FEDERAL_CITIES = {"Москва", "Санкт-Петербург", "Севастополь"}
_OSM_DAYS = {"Mo": "Пн", "Tu": "Вт", "We": "Ср", "Th": "Чт", "Fr": "Пт", "Sa": "Сб", "Su": "Вс"}


def osm_hours(value: str | None) -> str | None:
    """opening_hours из OSM по-русски: «Mo-Fr 10:00-20:00; Sa 10:00-18:00» ->
    «Пн–Пт 10:00–20:00, Сб 10:00–18:00»."""
    if not value:
        return None
    text = value.strip()
    if text == "24/7":
        return "круглосуточно"
    text = re.sub(r"\b(Mo|Tu|We|Th|Fr|Sa|Su)\b", lambda match: _OSM_DAYS[match.group(1)], text)
    text = re.sub(r"(?<=[\dа-яА-Я])-(?=[\dА-Я])", "–", text)
    return text.replace("; ", ", ").replace(";", ", ")[:256]


def _center(element: dict[str, Any]) -> tuple[float, float] | None:
    point = element.get("center") or element
    if "lat" not in point or "lon" not in point:
        return None
    return float(point["lat"]), float(point["lon"])


def _meters(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Расстояние по прямой — для соседних домов точности плоской формулы хватает."""
    dx = (a[1] - b[1]) * 111_320 * math.cos(math.radians(a[0]))
    dy = (a[0] - b[0]) * 110_540
    return math.hypot(dx, dy)


def _inside(point: tuple[float, float], polygon: list[tuple[float, float]]) -> bool:
    """Точка внутри контура дома (луч вправо считает пересечения)."""
    lat, lon = point
    inside = False
    for (lat1, lon1), (lat2, lon2) in zip(polygon, polygon[1:] + polygon[:1], strict=True):
        if (lat1 > lat) != (lat2 > lat):
            cross = lon1 + (lat - lat1) * (lon2 - lon1) / (lat2 - lat1)
            if lon < cross:
                inside = not inside
    return inside


class _Grid:
    """Сетка для поиска соседей без перебора всех объектов региона."""

    def __init__(self, cell_deg: float) -> None:
        self.cell = cell_deg
        self.items: dict[tuple[int, int], list[Any]] = {}

    def _key(self, point: tuple[float, float]) -> tuple[int, int]:
        return int(point[0] // self.cell), int(point[1] // self.cell)

    def add(self, point: tuple[float, float], item: Any) -> None:
        self.items.setdefault(self._key(point), []).append(item)

    def near(self, point: tuple[float, float]) -> list[Any]:
        row, col = self._key(point)
        found: list[Any] = []
        for d_row in (-1, 0, 1):
            for d_col in (-1, 0, 1):
                found.extend(self.items.get((row + d_row, col + d_col), ()))
        return found


def _house_address(tags: dict[str, str]) -> str | None:
    street = tags.get("addr:street") or tags.get("addr:place")
    house = tags.get("addr:housenumber")
    return f"{street}, {house}" if street and house else None


def ozon_rows_from_osm(elements: list[dict[str, Any]], region: str) -> list[PointRow]:
    """Пункты Ozon региона с адресами домов, в которых они стоят.

    В OSM пункт обычно отмечен точкой внутри здания, а адрес записан у самого
    здания. Адрес берём у дома, в контур которого попала точка, иначе у ближайшего
    дома в радиусе 60 м. Пункты без адреса пропускаем: ни клиенту, ни владелице
    они не помогут.
    """
    points, houses = [], _Grid(0.002)
    places = _Grid(0.25)
    for element in elements:
        tags = element.get("tags") or {}
        if tags.get("shop") == "outpost":
            points.append(element)
        elif "building" in tags and _house_address(tags):
            outline = [
                (float(node["lat"]), float(node["lon"])) for node in element.get("geometry") or []
            ]
            center = _center(element) or (outline[0] if outline else None)
            if center is not None:
                houses.add(center, (center, outline, tags))
        elif tags.get("place") and tags.get("name"):
            center = _center(element)
            if center is not None:
                places.add(center, (center, tags["name"]))

    rows = []
    for element in points:
        tags = element.get("tags") or {}
        position = _center(element)
        if position is None:
            continue
        address, house_tags = _house_address(tags), tags
        if address is None:
            nearby = houses.near(position)
            house = next((item for item in nearby if item[1] and _inside(position, item[1])), None)
            if house is None and nearby:
                closest = min(nearby, key=lambda item: _meters(position, item[0]))
                if _meters(position, closest[0]) <= _HOUSE_RADIUS_M:
                    house = closest
            if house is None:
                continue
            address, house_tags = _house_address(house[2]), house[2]

        city = tags.get("addr:city") or house_tags.get("addr:city")
        if region in _FEDERAL_CITIES:
            city = region
        if not city:
            nearby_places = places.near(position)
            if nearby_places:
                closest_place = min(nearby_places, key=lambda item: _meters(position, item[0]))
                if _meters(position, closest_place[0]) <= _PLACE_RADIUS_M:
                    city = closest_place[1]
        rows.append(
            PointRow(
                external_id=f"{element['type'][0]}{element['id']}",
                name="Пункт выдачи Ozon",
                city=(city or region)[:128],
                address=str(address)[:512],
                latitude=position[0],
                longitude=position[1],
                working_hours=osm_hours(tags.get("opening_hours")),
            ),
        )
    return rows


async def _overpass(client: httpx.AsyncClient, query: str) -> list[dict[str, Any]]:
    """Запрос к Overpass с переходом на запасной сервер и повтором при перегрузке."""
    last_error: Exception | None = None
    for attempt in range(3):
        for url in OVERPASS_URLS:
            try:
                response = await client.post(url, data={"data": query})
                if response.status_code in {429, 502, 503, 504}:
                    raise httpx.HTTPStatusError(
                        f"Overpass занят: {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                data = response.json()
                if "remark" in data and "error" in str(data["remark"]).lower():
                    raise ValueError(data["remark"])
                return data.get("elements") or []
            except (httpx.HTTPError, ValueError) as error:
                last_error = error
                logger.info("Overpass %s: %s", url, error)
        await asyncio.sleep(60 * (attempt + 1))
    raise ExternalServiceError(f"OpenStreetMap (Overpass) не ответил: {last_error}")


async def ozon_rows(failed: list[str]) -> AsyncIterator[list[PointRow]]:
    """Пункты Ozon из OpenStreetMap — по регионам России.

    Это данные волонтёров OpenStreetMap: часть пунктов Ozon в них нет, часть могла
    закрыться. Точку на карте и адрес владелица всё равно сверяет в приложении Ozon,
    когда оформляет отправку.
    """
    async with httpx.AsyncClient(timeout=_OVERPASS_TIMEOUT, headers=_OVERPASS_HEADERS) as client:
        regions = await _overpass(
            client,
            '[out:json][timeout:300];area["ISO3166-1"="RU"]["admin_level"="2"]->.ru;'
            'rel(area.ru)["boundary"="administrative"]["admin_level"="4"];out tags;',
        )
        for region in regions:
            name = (region.get("tags") or {}).get("name")
            if not name:
                continue
            area = 3_600_000_000 + int(region["id"])
            try:
                elements = await _overpass(
                    client,
                    f"[out:json][timeout:900];area({area})->.a;"
                    'nwr["shop"="outpost"]["brand"~"^(Ozon|OZON)"](area.a)->.p;'
                    ".p out center tags;"
                    f'way(around.p:{_HOUSE_RADIUS_M})["building"]["addr:housenumber"];out geom;'
                    'node(area.a)["place"~"^(city|town|village|hamlet)$"];out;',
                )
            except ExternalServiceError as error:
                # Регион не прочитался — остальные грузим, а этот отметим.
                logger.warning("Пункты Ozon, %s: %s", name, error.message)
                failed.append(name)
                continue
            rows = ozon_rows_from_osm(elements, name)
            del elements
            if rows:
                yield rows
            await asyncio.sleep(_OVERPASS_PAUSE_SECONDS)


# --- запись в базу ---

SOURCES = {
    DeliveryProviderCode.CDEK: cdek_rows,
    DeliveryProviderCode.YANDEX: yandex_rows,
    DeliveryProviderCode.OZON: ozon_rows,
}


async def _upsert(
    session: AsyncSession,
    provider: DeliveryProviderCode,
    environment: ProviderEnvironment,
    rows: list[PointRow],
    synced_at: dt.datetime,
) -> None:
    values = [
        {
            "provider": provider,
            "environment": environment,
            "external_id": row.external_id,
            "name": row.name,
            "city": row.city,
            "address": row.address,
            "latitude": row.latitude,
            "longitude": row.longitude,
            "working_hours": row.working_hours,
            "max_weight_g": row.max_weight_g,
            "is_active": True,
            "synced_at": synced_at,
            "search_text": normalize_search(f"{row.city} {row.address} {row.name}"),
        }
        for row in rows
    ]
    statement = insert(PickupPoint).values(values)
    columns = (
        "name",
        "city",
        "address",
        "latitude",
        "longitude",
        "working_hours",
        "max_weight_g",
        "is_active",
        "synced_at",
        "search_text",
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=["provider", "environment", "external_id"],
            set_={column: statement.excluded[column] for column in columns}
            | {"updated_at": synced_at},
        ),
    )


async def sync_provider(provider: DeliveryProviderCode) -> Counter:
    """Обновить справочник одной службы.

    Сначала собираем все пункты из источника (это минуты, а у Ozon — часы: Overpass
    опрашивается по регионам), и только потом одной короткой транзакцией пишем в базу:
    долгая открытая транзакция держала бы соединение и строки всё это время.

    Если часть источника не ответила (регион Overpass), записываем то, что получили,
    но ничего не выключаем — иначе пропали бы пункты непрочитанного региона.
    """
    environment = get_settings().delivery_environment
    stats: Counter = Counter()
    failed: list[str] = []
    rows: list[PointRow] = []
    try:
        async for batch in SOURCES[provider](failed):
            rows.extend(batch)
        stats["получено"] = len(rows)
        synced_at = now_utc()

        async with session_scope() as session:
            known = await session.scalar(
                select(func.count(PickupPoint.id)).where(
                    PickupPoint.provider == provider,
                    PickupPoint.environment == environment,
                    PickupPoint.is_active.is_(True),
                ),
            )
            if not failed and len(rows) < (known or 0) * MIN_SHARE_OF_KNOWN:
                raise ExternalServiceError(
                    f"источник отдал {len(rows)} пунктов при {known} известных — "
                    "похоже, он сломался; ничего не выключаем",
                )
            # У Postgres предел в 32 767 параметров на запрос: пишем кусками.
            for start in range(0, len(rows), BATCH_SIZE):
                chunk = rows[start : start + BATCH_SIZE]
                await _upsert(session, provider, environment, chunk, synced_at)

            note = None
            if failed:
                stats["не прочитано частей"] = len(failed)
                note = (
                    "обновлено не полностью — не ответили: "
                    + ", ".join(failed[:10])
                    + ("…" if len(failed) > 10 else "")
                    + ". Старые пункты оставлены как были."
                )
            else:
                result = await session.execute(
                    update(PickupPoint)
                    .where(
                        PickupPoint.provider == provider,
                        PickupPoint.environment == environment,
                        PickupPoint.is_active.is_(True),
                        (PickupPoint.synced_at.is_(None)) | (PickupPoint.synced_at < synced_at),
                    )
                    .values(is_active=False),
                )
                stats["выключено"] = result.rowcount or 0
            await session.execute(
                update(DeliveryProvider)
                .where(DeliveryProvider.code == provider)
                .values(pickup_points_synced_at=synced_at, last_sync_error=note),
            )
    except ExternalServiceError as error:
        await _remember_error(provider, error.message)
        raise
    return stats


async def _remember_error(provider: DeliveryProviderCode, message: str) -> None:
    """Ошибку видно в панели, в разделе «Доставка»."""
    async with session_scope() as session:
        await session.execute(
            update(DeliveryProvider)
            .where(DeliveryProvider.code == provider)
            .values(last_sync_error=message[:1000]),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Обновить пункты выдачи из публичных источников")
    parser.add_argument(
        "providers",
        nargs="+",
        choices=[code.value for code in SOURCES],
    )
    args = parser.parse_args()

    async def run() -> None:
        try:
            for code in args.providers:
                stats = await sync_provider(DeliveryProviderCode(code))
                summary = ", ".join(f"{name}: {count}" for name, count in stats.items())
                print(f"{code}: {summary}")  # noqa: T201 — консольная команда
        finally:
            await dispose_engine()

    asyncio.run(run())


if __name__ == "__main__":
    main()
