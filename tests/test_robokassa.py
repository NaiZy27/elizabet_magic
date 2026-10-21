"""Робокасса: сумма, чек по услугам, подписи и сверка."""

from __future__ import annotations

import hashlib
import json
from urllib.parse import quote

import httpx
import pytest
from pydantic import SecretStr

from core.config import get_settings
from core.enums import AddonChargeMode
from core.errors import ExternalServiceError
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


def test_checkout_form_signature(shop):
    order = box_with_video()
    payment = Payment(id=42, amount_kopecks=200000)

    params = robokassa.checkout_fields(payment, order)

    # В поле формы — чек, закодированный один раз: второй раз его закодирует браузер.
    receipt = quote(robokassa.receipt_json(order, 200000), safe="")
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


# --- сверка: OpStateExt ---

OP_STATE_PAID = """<?xml version="1.0" encoding="utf-8"?>
<OperationStateResponse xmlns:xsd="http://www.w3.org/2001/XMLSchema"
    xmlns="http://merchant.roboxchange.com/WebService/">
  <Result><Code>0</Code></Result>
  <State><Code>100</Code><RequestDate>2026-10-09T10:00:00+03:00</RequestDate></State>
  <Info>
    <IncCurrLabel>BankCardPSR</IncCurrLabel>
    <IncSum>2000.000000</IncSum>
    <PaymentMethod><Code>BankCard</Code><Description>Банковская карта</Description></PaymentMethod>
    <OutSum>1940.000000</OutSum>
    <OpKey>0005F891-8CCD-434B-8455-816AFFFDBF37-0VOisWikFF</OpKey>
  </Info>
</OperationStateResponse>"""


def test_op_state_signature_uses_password2(shop):
    assert robokassa.op_state_signature(42) == hashlib.md5(b"Boxru:42:p2").hexdigest()


def test_parse_paid_operation():
    state = robokassa.parse_operation_state(OP_STATE_PAID)

    assert state is not None and state.is_paid
    assert state.inc_sum == "2000.000000"
    assert state.payment_method == "BankCard"


def test_parse_unknown_operation_and_errors():
    not_found = "<OperationStateResponse><Result><Code>3</Code></Result></OperationStateResponse>"
    assert robokassa.parse_operation_state(not_found) is None

    bad_sign = (
        "<OperationStateResponse><Result><Code>1</Code>"
        "<Description>Неверная подпись</Description></Result></OperationStateResponse>"
    )
    with pytest.raises(ExternalServiceError, match="Неверная подпись"):
        robokassa.parse_operation_state(bad_sign)
    with pytest.raises(ExternalServiceError):
        robokassa.parse_operation_state("<html>502</html")


def test_checkout_remembers_whether_receipt_went_to_robocheks(shop):
    from core.services import payments

    order = box_with_video()
    full = Payment(id=42, amount_kopecks=200000)
    payments.mark_checkout_started(full, order)
    assert full.receipt_sent and full.checkout_started_at is not None

    # Строки чека не сходятся с суммой — чек в кассу не уходит, его пробьют вручную.
    odd = Payment(id=43, amount_kopecks=199900)
    payments.mark_checkout_started(odd, order)
    assert not odd.receipt_sent


#: Настоящий клиент: подмена ниже меняет httpx.AsyncClient для всего процесса.
_REAL_CLIENT = httpx.AsyncClient


async def test_operation_state_request(shop, monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=OP_STATE_PAID)

    def client(**kwargs):
        return _REAL_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(robokassa.httpx, "AsyncClient", client)
    state = await robokassa.operation_state(42)

    assert state is not None and state.is_paid
    params = seen[0].url.params
    assert (params["MerchantLogin"], params["InvoiceID"]) == ("Boxru", "42")
    assert params["Signature"] == robokassa.op_state_signature(42)


def test_test_mode_payment_is_reissued_in_production(shop, monkeypatch):
    """Счёт из тестового режима в боевом не оплачивают: номер мог уйти в тестовую операцию."""
    from core.enums import PaymentProvider, ProviderEnvironment
    from core.services import payments

    monkeypatch.setattr(get_settings(), "payments_mode", "robokassa")
    from_test = Payment(provider=PaymentProvider.ROBOKASSA, environment=ProviderEnvironment.TEST)
    live = Payment(provider=PaymentProvider.ROBOKASSA, environment=ProviderEnvironment.PRODUCTION)
    stub = Payment(provider=PaymentProvider.STUB, environment=ProviderEnvironment.TEST)

    assert payments.is_current(live)
    assert not payments.is_current(from_test)
    assert not payments.is_current(stub)
    monkeypatch.setattr(shop, "is_test", True)
    assert payments.is_current(from_test)


def test_test_amount_charges_one_ruble_per_receipt_line(shop, monkeypatch):
    """Режим проверки: каждая строка чека по 1 ₽ — бокс, видео, доставка = 3 ₽."""
    from core.services import payments, receipts

    monkeypatch.setattr(get_settings(), "payments_test_amount_kopecks", 100)
    order = box_with_video()

    assert payments.payable_kopecks(order) == 300
    fields = payments.mark_checkout_started(Payment(id=42, amount_kopecks=300), order)
    assert fields["OutSum"] == "3.00"
    items = json.loads(robokassa.receipt_json(order, 300))["items"]
    assert [(item["name"], item["sum"]) for item in items] == [
        ("Маленький бокс — 3 ложечки", 1.0),
        ("Видео сборки", 1.0),
        ("Доставка до пункта выдачи", 1.0),
    ]
    # В базе чек с теми же суммами, что ушли в Робочеки.
    assert [line["amount_kopecks"] for line in receipts.receipt_lines(order)] == [100, 100, 100]

    monkeypatch.setattr(get_settings(), "payments_test_amount_kopecks", None)
    assert payments.payable_kopecks(order) == 200000
