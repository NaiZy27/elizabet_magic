"""Робокасса: сумма, чек по услугам и подписи."""

from __future__ import annotations

import hashlib
import json
from urllib.parse import parse_qs, quote, urlsplit

import pytest
from pydantic import SecretStr

from core.config import get_settings
from core.enums import AddonChargeMode
from core.models import Order, OrderAddon, OrderItem, Payment
from core.services import robokassa


@pytest.fixture
def shop(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings.robokassa, "login", "Boxru")
    monkeypatch.setattr(settings.robokassa, "password1", SecretStr("p1"))
    monkeypatch.setattr(settings.robokassa, "password2", SecretStr("p2"))
    monkeypatch.setattr(settings.robokassa, "hash_algo", "md5")
    monkeypatch.setattr(settings.robokassa, "is_test", False)
    return settings.robokassa


def box_with_video() -> Order:
    """Бокс 3 ложечки — 1200 ₽ + видео 300 ₽ + доставка 500 ₽ = 2000 ₽."""
    order = Order(
        id=7,
        subtotal_kopecks=150000,
        delivery_kopecks=50000,
        total_kopecks=200000,
        recipient_email="anna@mail.ru",
    )
    item = OrderItem(
        name_snapshot="Маленький бокс — 3 ложечки", unit_price_kopecks=120000, quantity=1
    )
    video = OrderAddon(
        name_snapshot="Видео сборки",
        unit_price_kopecks=30000,
        quantity=1,
        code_snapshot="video",
        charge_mode_snapshot=AddonChargeMode.PER_BOX,
    )
    item.addons = [video]
    order.items = [item]
    order.addons = [video]
    return order


def test_out_sum_format():
    assert robokassa.format_out_sum(150000) == "1500.00"
    assert robokassa.format_out_sum(5) == "0.05"
    assert robokassa.parse_out_sum("1500.000000") == 150000


def test_receipt_lists_every_service_separately():
    items = robokassa.receipt_items(box_with_video())

    assert [(item["name"], item["sum"]) for item in items] == [
        ("Маленький бокс — 3 ложечки", 1200.0),
        ("Видео сборки", 300.0),
        ("Доставка до пункта выдачи", 500.0),
    ]
    assert {item["tax"] for item in items} == {"none"}


def test_receipt_dropped_when_sum_differs():
    assert robokassa.receipt_json(box_with_video(), 199900) is None


def test_payment_link_signature(shop):
    order = box_with_video()
    payment = Payment(id=42, amount_kopecks=200000)

    url = robokassa.payment_link(payment, order)
    params = {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}

    receipt = quote(robokassa.receipt_json(order, 200000), safe="")
    # parse_qs снимает одно кодирование — остаётся чек, закодированный один раз.
    assert params["Receipt"] == receipt
    expected = hashlib.md5(f"Boxru:2000.00:42:{receipt}:p1".encode()).hexdigest()
    assert params["SignatureValue"] == expected
    assert params["OutSum"] == "2000.00"
    assert "IsTest" not in params
    assert json.loads(robokassa.receipt_json(order, 200000))["items"][1]["name"] == "Видео сборки"


def test_result_signature_uses_password2(shop):
    good = hashlib.md5(b"2000.000000:42:p2").hexdigest().upper()

    assert robokassa.check_result_signature("2000.000000", "42", good)
    assert not robokassa.check_result_signature("2000.000000", "43", good)
    assert not robokassa.check_success_signature("2000.000000", "42", good)


def test_bot_link_goes_straight_to_robokassa(monkeypatch):
    """Кнопка в боте ведёт на /go — оттуда сразу редирект в Робокассу, без страницы заказа."""
    from core.enums import PaymentProvider
    from core.services import payments

    monkeypatch.setattr(get_settings(), "public_base_url", "https://shop.ru")
    token = "6f1c2d3e-0000-4000-8000-000000000001"
    real = Payment(public_token=token, provider=PaymentProvider.ROBOKASSA)
    stub = Payment(public_token=token, provider=PaymentProvider.STUB)
    assert payments.payment_url(real) == f"https://shop.ru/pay/{token}/go"
    assert payments.payment_url(stub) == f"https://shop.ru/pay/{token}"
