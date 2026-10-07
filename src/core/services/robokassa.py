"""Робокасса: ссылка на оплату, чек и проверка подписей.

Как идут деньги:
1. Клиент со страницы /pay/<токен> уходит на auth.robokassa.ru по ссылке, которую
   собирает `payment_link`. В ссылке — сумма, номер счёта (id платежа) и чек:
   каждая услуга отдельной строкой, чтобы Робочеки СМЗ передали в «Мой налог»
   ровно то, что купил клиент.
2. После оплаты Робокасса стучится на ResultURL (`/webhooks/robokassa/result`).
   Только это уведомление с верной подписью (пароль №2) проводит оплату.
3. Клиента Робокасса возвращает на SuccessURL или FailURL — это просто переходы
   браузера, оплату они не подтверждают.

Адреса ResultURL / SuccessURL / FailURL и алгоритм подписи задаются в кабинете
магазина, в «Технических настройках». Подпись считается тем же алгоритмом, что
указан там (ROBOKASSA_HASH_ALGO).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote, urlencode

from core.config import get_settings
from core.errors import ValidationError
from core.models import Order, Payment

#: Описание платежа на странице Робокассы (до 100 символов).
PAYMENT_DESCRIPTION = "Подарочные боксы Elizabet Magic"

#: Название позиции в чеке — не длиннее 128 символов.
_RECEIPT_NAME_LIMIT = 128


def format_out_sum(kopecks: int) -> str:
    """Сумма так, как её ждёт Робокасса: рубли с двумя знаками, точка — разделитель."""
    return f"{kopecks // 100}.{kopecks % 100:02d}"


def parse_out_sum(value: str) -> int:
    """«1500.000000» из уведомления -> 150000 копеек."""
    try:
        amount = Decimal(value.strip().replace(",", "."))
    except (InvalidOperation, AttributeError):
        raise ValidationError(f"Неверная сумма в уведомлении: {value!r}") from None
    return int((amount * 100).quantize(Decimal("1")))


def receipt_items(order: Order) -> list[dict[str, Any]]:
    """Позиции чека: бокс, каждая его услуга и доставка — отдельными строками.

    Так клиент видит в чеке то же, что в боте: «бокс 3 ложечки — 1 200 ₽,
    видео сборки — 300 ₽, доставка — 500 ₽».
    """
    items: list[dict[str, Any]] = []

    def add(name: str, kopecks: int, kind: str) -> None:
        if kopecks <= 0:
            return
        items.append(
            {
                "name": name[:_RECEIPT_NAME_LIMIT],
                "quantity": 1,
                "sum": float(Decimal(kopecks) / 100),
                "payment_method": "full_payment",
                "payment_object": kind,
                "tax": "none",
            },
        )

    for item in order.items:
        add(item.name_snapshot, item.unit_price_kopecks * item.quantity, "commodity")
        for addon in item.addons:
            add(addon.name_snapshot, addon.total_kopecks, "service")
    for addon in order.order_addons:
        add(addon.name_snapshot, addon.total_kopecks, "service")
    add("Доставка до пункта выдачи", order.delivery_kopecks, "service")
    return items


def receipt_json(order: Order, amount_kopecks: int) -> str | None:
    """Чек одной строкой JSON. None — если строки не сходятся с суммой платежа.

    Робокасса отклонит оплату, где сумма чека не равна OutSum, поэтому при
    расхождении (например, заказ поменяли руками в панели) чек не передаём вовсе:
    оплата пройдёт, а чек владелица пробьёт сама.
    """
    items = receipt_items(order)
    total = sum(round(item["sum"] * 100) for item in items)
    if not items or total != amount_kopecks:
        return None
    return json.dumps({"items": items}, ensure_ascii=False, separators=(",", ":"))


def _hash(value: str) -> str:
    algo = get_settings().robokassa.hash_algo
    return hashlib.new(algo, value.encode("utf-8")).hexdigest()


def _password1() -> str:
    return get_settings().robokassa.password1.get_secret_value()


def _password2() -> str:
    return get_settings().robokassa.password2.get_secret_value()


def payment_link(payment: Payment, order: Order) -> str:
    """Ссылка на оплату в Робокассе для этой попытки.

    Чек в подписи участвует URL-кодированным один раз, а в самой ссылке он
    кодируется ещё раз — так требует Робокасса для GET-запроса.
    """
    settings = get_settings().robokassa
    out_sum = format_out_sum(payment.amount_kopecks)
    inv_id = str(payment.id)

    receipt = receipt_json(order, payment.amount_kopecks)
    receipt_encoded = quote(receipt, safe="") if receipt else None

    parts = [settings.login, out_sum, inv_id]
    if receipt_encoded:
        parts.append(receipt_encoded)
    parts.append(_password1())
    signature = _hash(":".join(parts))

    params: dict[str, str] = {
        "MerchantLogin": settings.login,
        "OutSum": out_sum,
        "InvId": inv_id,
        "Description": PAYMENT_DESCRIPTION,
        "SignatureValue": signature,
        "Culture": "ru",
        "Encoding": "utf-8",
    }
    if receipt_encoded:
        params["Receipt"] = receipt_encoded
    if order.recipient_email:
        params["Email"] = order.recipient_email
    if settings.is_test:
        params["IsTest"] = "1"
    return f"{settings.payment_url}?{urlencode(params)}"


def _signature_matches(expected_source: str, received: str) -> bool:
    return hmac.compare_digest(_hash(expected_source).lower(), (received or "").strip().lower())


def check_result_signature(out_sum: str, inv_id: str, signature: str) -> bool:
    """Подпись уведомления ResultURL: OutSum:InvId:Пароль#2."""
    return _signature_matches(f"{out_sum}:{inv_id}:{_password2()}", signature)


def check_success_signature(out_sum: str, inv_id: str, signature: str) -> bool:
    """Подпись возврата клиента на SuccessURL: OutSum:InvId:Пароль#1."""
    return _signature_matches(f"{out_sum}:{inv_id}:{_password1()}", signature)
