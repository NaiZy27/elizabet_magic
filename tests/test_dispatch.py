"""Ручная отправка и цены доставки: телефон, данные для владелицы, Москва и Россия."""

from __future__ import annotations

import pytest

from core.enums import DeliveryProviderCode
from core.models import DeliveryProvider, Order, OrderItem
from core.services import delivery, shipments
from core.text import phone_e164, phone_pretty


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


def _cdek() -> DeliveryProvider:
    return DeliveryProvider(
        code=DeliveryProviderCode.CDEK,
        name="СДЭК",
        moscow_price_kopecks=30000,
        region_price_kopecks=50000,
    )


def test_price_depends_on_moscow():
    provider = _cdek()
    assert delivery.price_for_city(provider, "Москва") == 30000
    assert delivery.price_for_city(provider, "москва") == 30000
    assert delivery.price_for_city(provider, "Химки") == 50000
    assert delivery.price_for_city(provider, None) == 50000


def test_price_line_in_bot():
    from bot.emoji import provider_price_line

    provider = _cdek()
    assert provider_price_line(provider).endswith("СДЭК — по Москве 300\xa0₽, по России 500\xa0₽")
    provider.moscow_price_kopecks = 50000
    assert provider_price_line(provider).endswith("СДЭК — 500\xa0₽")


def _ozon_order() -> Order:
    order = Order(
        id=5,
        number="EM-1005",
        recipient_name="Анна <Иванова>",
        recipient_phone="8 900 123-45-67",
        delivery_provider=DeliveryProviderCode.OZON,
        pickup_snapshot={
            "provider": "ozon",
            "provider_name": "Ozon",
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
    text = shipments.manual_dispatch_text(_ozon_order())

    assert "отправьте через Ozon" in text
    assert "<code>Москва, Нагорная улица, д. 15к1-2</code> · код <code>2500</code>" in text
    assert "<code>+7 900 123-45-67</code>" in text
    assert "Пн–Вс 10:00–21:00" in text
    assert "Маленький бокс — 2 ложечки" in text
    # Имя клиента — чужой ввод: в HTML-сообщение попадает экранированным.
    assert "Анна &lt;Иванова&gt;" in text


def test_owner_card_says_send_manually():
    lines = shipments.owner_delivery_lines(_ozon_order())
    assert lines[0] == "🚚 Ozon · отправить вручную, когда заказ будет готов"


def test_owner_sees_customer_telegram():
    from core.models import Customer

    order = _ozon_order()
    order.customer = Customer(telegram_user_id=42, username="anna_box", full_name="Анна")
    assert "💬 Telegram: @anna_box" in shipments.manual_dispatch_text(order)
    assert "💬 @anna_box" in shipments.owner_delivery_lines(order)

    order.customer = Customer(telegram_user_id=42, username=None, full_name="Анна")
    assert '<a href="tg://user?id=42">Анна</a> (без ника)' in shipments.manual_dispatch_text(order)


def test_manual_address_is_shown_as_typed():
    order = _ozon_order()
    order.pickup_snapshot = {
        "provider": "ozon",
        "provider_name": "Ozon",
        "external_id": None,
        "city": "",
        "address": "Казань, ул. Баумана, 38",
        "manual": True,
    }
    text = shipments.manual_dispatch_text(order)
    assert "<code>Казань, ул. Баумана, 38</code>" in text
    assert "код" not in text.split("Пункт:")[1].split("\n")[0]
    assert shipments.MANUAL_NOTE in text
