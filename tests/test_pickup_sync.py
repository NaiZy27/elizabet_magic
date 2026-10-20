"""Справочник пунктов Яндекса: разбор источника и часы работы."""

from __future__ import annotations

import datetime as dt

from core.services import pickup_sync


def test_week_groups_same_hours():
    week = dict.fromkeys(range(5), "10:00–21:00") | {5: "10:00–18:00", 6: None}
    assert pickup_sync.format_week(week) == "Пн–Пт 10:00–21:00, Сб 10:00–18:00"
    assert pickup_sync.format_week({}) is None


def _yandex_point(**overrides):
    point = {
        "id": "0196abc",
        "name": "Пункт выдачи заказов Яндекс Маркета",
        "type": "pickup_point",
        "position": {"latitude": 44.1, "longitude": 39.07},
        "address": {
            "country": "Россия",
            "locality": "Туапсе г",
            "street": "Судоремонтников ул.",
            "house": "58",
            "housing": "",
            "building": "",
            "full_address": "Туапсе г Судоремонтников ул. 58",
        },
        "schedule": {
            "restrictions": [
                {
                    "days": [1, 2, 3, 4, 5, 6, 7],
                    "time_from": {"hours": 9, "minutes": 0},
                    "time_to": {"hours": 21, "minutes": 0},
                },
            ],
        },
        "deactivation_date": None,
    }
    return point | overrides


def test_yandex_row():
    today = dt.date(2026, 10, 18)
    row = pickup_sync.yandex_row(_yandex_point(), today=today)
    assert (row.external_id, row.city, row.address) == (
        "0196abc",
        "Туапсе",
        "Судоремонтников ул., 58",
    )
    assert row.working_hours == "Пн–Вс 09:00–21:00"

    # Закрывается через неделю — не предлагаем; через два месяца — можно.
    assert (
        pickup_sync.yandex_row(_yandex_point(deactivation_date="2026-10-25"), today=today) is None
    )
    assert pickup_sync.yandex_row(_yandex_point(deactivation_date="2026-12-20"), today=today)
    # Постамат и пункт не в России не берём.
    assert pickup_sync.yandex_row(_yandex_point(type="terminal"), today=today) is None
    point = _yandex_point()
    point["address"] = point["address"] | {"country": "Беларусь"}
    assert pickup_sync.yandex_row(point, today=today) is None


def _cdek_point(**overrides):
    point = {
        "code": "MSK1005",
        "status": "ACTIVE",
        "countryCodeIso": "RU",
        "city": "Москва",
        "address": "ул. Тверская, 7",
        "coordX": "37.61",
        "coordY": "55.76",
        "type": "PVZ",
        "ownerCode": "CDEK",
        "isHandout": True,
        "workTime": "Пн-Пт 10:00-20:00, Сб-Вс 10:00-18:00",
        "workTimeYList": [
            {"day": str(day), "periods": "10:00/20:00" if day <= 5 else "10:00/18:00"}
            for day in range(1, 8)
        ],
        "weightLimit": {"WeightMin": "0.0", "WeightMax": "30.0"},
    }
    return point | overrides


def test_cdek_row():
    row = pickup_sync.cdek_row(_cdek_point())
    assert (row.external_id, row.city, row.address) == ("MSK1005", "Москва", "ул. Тверская, 7")
    assert (row.latitude, row.longitude) == (55.76, 37.61)
    assert row.working_hours == "Пн–Пт 10:00–20:00, Сб–Вс 10:00–18:00"
    assert row.max_weight_g == 30000

    # Только собственные офисы СДЭК в России, где выдают посылки; без постаматов.
    assert pickup_sync.cdek_row(_cdek_point(ownerCode="Почта России")) is None
    assert pickup_sync.cdek_row(_cdek_point(ownerCode="Вайлдберриз")) is None
    assert pickup_sync.cdek_row(_cdek_point(type="POSTAMAT")) is None
    assert pickup_sync.cdek_row(_cdek_point(countryCodeIso="KZ")) is None
    assert pickup_sync.cdek_row(_cdek_point(isHandout=False)) is None


def test_osm_hours():
    assert pickup_sync.osm_hours("Mo-Fr 10:00-20:00; Sa 10:00-18:00") == (
        "Пн–Пт 10:00–20:00, Сб 10:00–18:00"
    )
    assert pickup_sync.osm_hours("Mo-Su 09:00-21:00") == "Пн–Вс 09:00–21:00"
    assert pickup_sync.osm_hours("24/7") == "круглосуточно"
    assert pickup_sync.osm_hours(None) is None


def _house(osm_id, street, house, corner, size=0.0004, **tags):
    lat, lon = corner
    outline = [
        {"lat": lat, "lon": lon},
        {"lat": lat + size, "lon": lon},
        {"lat": lat + size, "lon": lon + size},
        {"lat": lat, "lon": lon + size},
    ]
    return {
        "type": "way",
        "id": osm_id,
        "center": {"lat": lat + size / 2, "lon": lon + size / 2},
        "geometry": outline,
        "tags": {"building": "yes", "addr:street": street, "addr:housenumber": house} | tags,
    }


def _outpost(osm_id, lat, lon, **tags):
    return {
        "type": "node",
        "id": osm_id,
        "lat": lat,
        "lon": lon,
        "tags": {"shop": "outpost", "brand": "Ozon", "opening_hours": "Mo-Su 10:00-22:00"} | tags,
    }


def test_ozon_point_gets_address_of_its_building():
    elements = [
        _house(1, "Тверская улица", "7", (55.7600, 37.6100)),
        _house(2, "Тверская улица", "9", (55.7610, 37.6100)),
        _outpost(10, 55.76015, 37.61015),  # внутри дома 7
        _outpost(11, 55.76150, 37.61010),  # снаружи, в ~30 м от дома 9
        _outpost(12, 55.70000, 37.50000),  # вокруг домов с адресом нет — пропускаем
    ]
    rows = {row.external_id: row for row in pickup_sync.ozon_rows_from_osm(elements, "Москва")}

    assert set(rows) == {"n10", "n11"}
    assert rows["n10"].address == "Тверская улица, 7"
    assert rows["n11"].address == "Тверская улица, 9"
    assert rows["n10"].city == "Москва"
    assert rows["n10"].working_hours == "Пн–Вс 10:00–22:00"


def test_ozon_city_from_nearest_place_outside_federal_cities():
    elements = [
        _house(1, "улица Ленина", "1", (56.8580, 35.9000)),
        _outpost(10, 56.85815, 35.90015),
        {
            "type": "node",
            "id": 50,
            "lat": 56.86,
            "lon": 35.90,
            "tags": {"place": "city", "name": "Тверь"},
        },
    ]
    (row,) = pickup_sync.ozon_rows_from_osm(elements, "Тверская область")
    assert (row.city, row.address) == ("Тверь", "улица Ленина, 1")
