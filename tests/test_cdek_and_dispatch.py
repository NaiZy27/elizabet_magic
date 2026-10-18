"""СДЭК по API и ручная отправка: телефон, статусы, тариф, данные для владелицы."""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr

from core.config import get_settings
from core.enums import DeliveryProviderCode, ShipmentStatus
from core.models import Order, OrderItem
from core.services import cdek, delivery, shipments
from core.text import phone_e164, phone_pretty

#: Настоящий клиент: подмена ниже меняет httpx.AsyncClient для всего процесса.
_REAL_CLIENT = httpx.AsyncClient


class FakeRedis:
    """Кэш в памяти вместо Redis."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.data[key] = value


@pytest.fixture
def cdek_api(monkeypatch):
    """СДЭК с ключами, пунктом сдачи MOS4 и подменённой сетью."""
    settings = get_settings().cdek
    monkeypatch.setattr(settings, "client_id", "id")
    monkeypatch.setattr(settings, "client_secret", SecretStr("secret"))
    monkeypatch.setattr(settings, "shipment_point", "MOS4")
    monkeypatch.setattr(settings, "base_url", "https://cdek.test/v2")
    redis = FakeRedis()
    monkeypatch.setattr(cdek, "cache_redis", lambda: redis)

    calls: list[httpx.Request] = []
    cities = {"MOS4": (44, "Москва", "ул. Островитянова, 11"), "SPB1": (137, "СПб", "Невский, 1")}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if path.endswith("/oauth/token"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if path.endswith("/deliverypoints"):
            code = request.url.params["code"]
            city_code, city, address = cities[code]
            return httpx.Response(
                200,
                json=[
                    {
                        "code": code,
                        "location": {"city_code": city_code, "city": city, "address": address},
                    }
                ],
            )
        if path.endswith("/calculator/tariff"):
            return httpx.Response(200, json={"total_sum": 234.6, "period_min": 1, "period_max": 2})
        return httpx.Response(404, json={"errors": [{"message": "нет такого"}]})

    def client(**kwargs):
        return _REAL_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(cdek.httpx, "AsyncClient", client)
    return calls


@pytest.mark.parametrize(
    ("raw", "e164", "pretty"),
    [
        ("8 900 123-45-67", "+79001234567", "+7 900 123-45-67"),
        ("+7(900)1234567", "+79001234567", "+7 900 123-45-67"),
        ("9001234567", "+79001234567", "+7 900 123-45-67"),
        ("12345", None, "12345"),
    ],
)
def test_phone_formats(raw, e164, pretty):
    assert phone_e164(raw) == e164
    assert phone_pretty(raw) == pretty


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("CREATED", ShipmentStatus.CREATED),
        ("ACCEPTED", ShipmentStatus.CREATED),
        ("RECEIVED_AT_SHIPMENT_WAREHOUSE", ShipmentStatus.IN_TRANSIT),
        ("SENT_TO_RECIPIENT_CITY", ShipmentStatus.IN_TRANSIT),
        ("ACCEPTED_AT_PICK_UP_POINT", ShipmentStatus.ARRIVED),
        ("ACCEPTED_AT_WAREHOUSE_ON_DEMAND", ShipmentStatus.ARRIVED),
        ("DELIVERED", ShipmentStatus.DELIVERED),
        ("NOT_DELIVERED", ShipmentStatus.RETURNED),
        ("REMOVED", ShipmentStatus.CANCELLED),
        ("SOMETHING_NEW", ShipmentStatus.IN_TRANSIT),
    ],
)
def test_cdek_status_mapping(code, status):
    assert cdek.map_status(code) == status


def test_parse_order_takes_latest_status_and_number():
    info = cdek.parse_order(
        {
            "entity": {
                "uuid": "u-1",
                "cdek_number": 1110033817,
                "statuses": [
                    {"code": "ACCEPTED", "name": "Принят", "date_time": "2026-10-18T13:36:00+0000"},
                    {"code": "CREATED", "name": "Создан", "date_time": "2026-10-18T13:35:58+0000"},
                ],
            },
            "requests": [{"type": "CREATE", "state": "SUCCESSFUL"}],
        },
    )
    assert (info.cdek_number, info.status_code, info.create_state) == (
        "1110033817",
        "ACCEPTED",
        "SUCCESSFUL",
    )

    invalid = cdek.parse_order(
        {
            "entity": {"uuid": "u-2"},
            "requests": [
                {"type": "CREATE", "state": "INVALID", "errors": [{"message": "Неверный ПВЗ"}]},
            ],
        },
    )
    assert invalid.create_state == "INVALID" and invalid.errors == ("Неверный ПВЗ",)


async def test_calculate_rounds_up_and_caches(cdek_api):
    tariff = await cdek.calculate(
        to_point="SPB1",
        weight_g=500,
        length_cm=20,
        width_cm=15,
        height_cm=10,
        declared_kopecks=140000,
    )
    assert (tariff.price_kopecks, tariff.days_min, tariff.days_max) == (23500, 1, 2)

    calculator = [call for call in cdek_api if call.url.path.endswith("/calculator/tariff")]
    body = json.loads(calculator[0].content)
    assert body["tariff_code"] == 136 and body["type"] == 1
    assert (body["from_location"]["code"], body["to_location"]["code"]) == (44, 137)
    assert body["services"] == [{"code": "INSURANCE", "parameter": "1400"}]

    again = await cdek.calculate(
        to_point="SPB1",
        weight_g=500,
        length_cm=20,
        width_cm=15,
        height_cm=10,
        declared_kopecks=140000,
    )
    assert again == tariff
    assert len([c for c in cdek_api if c.url.path.endswith("/calculator/tariff")]) == 1


def test_cdek_is_automatic_only_with_keys_and_point(monkeypatch):
    settings = get_settings().cdek
    monkeypatch.setattr(settings, "client_id", "")
    assert not delivery.is_automatic(DeliveryProviderCode.CDEK)
    monkeypatch.setattr(settings, "client_id", "id")
    monkeypatch.setattr(settings, "client_secret", SecretStr("secret"))
    monkeypatch.setattr(settings, "shipment_point", "MOS4")
    assert delivery.is_automatic(DeliveryProviderCode.CDEK)
    assert not delivery.is_automatic(DeliveryProviderCode.WB)
    assert not delivery.is_automatic(DeliveryProviderCode.YANDEX)


def _wb_order() -> Order:
    order = Order(
        id=5,
        number="EM-1005",
        recipient_name="Анна <Иванова>",
        recipient_phone="8 900 123-45-67",
        delivery_provider=DeliveryProviderCode.WB,
        pickup_snapshot={
            "provider": "wb",
            "provider_name": "Wildberries",
            "external_id": "2500",
            "city": "Москва",
            "address": "Нагорная улица, д. 15к1-2",
            "working_hours": "Пн–Вс 10:00–21:00",
        },
    )
    item = OrderItem(
        name_snapshot="Маленький бокс — 2 ложечки", quantity=1, unit_price_kopecks=90000
    )
    item.addons = []
    order.items = [item]
    order.addons = []
    return order


def test_manual_dispatch_text_has_everything_to_copy():
    text = shipments.manual_dispatch_text(_wb_order())

    assert "отправьте вручную через Wildberries" in text
    assert "<code>Москва, Нагорная улица, д. 15к1-2</code> · код <code>2500</code>" in text
    assert "<code>+7 900 123-45-67</code>" in text
    assert "Пн–Вс 10:00–21:00" in text
    assert "Маленький бокс — 2 ложечки" in text
    # Имя клиента — чужой ввод: в HTML-сообщение попадает экранированным.
    assert "Анна &lt;Иванова&gt;" in text


def test_owner_card_says_how_order_will_be_sent(monkeypatch):
    monkeypatch.setattr(get_settings().cdek, "client_id", "")
    lines = shipments.owner_delivery_lines(_wb_order())
    assert lines[0] == "🚚 Wildberries · отправить вручную"
