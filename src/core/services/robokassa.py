"""Робокасса: форма оплаты, чек и проверка подписей.

Как идут деньги:
1. Со страницы /pay/<токен>/go браузер клиента отправляет POST-форму на
   auth.robokassa.ru — поля собирает `checkout_fields`. В форме сумма, номер счёта
   (id платежа) и чек: каждая услуга отдельной строкой, чтобы Робочеки СМЗ передали
   в «Мой налог» ровно то, что купил клиент. Именно POST, а не ссылка: чек с парой
   боксов раздувает адрес до нескольких тысяч символов, и Робокасса прямо просит
   передавать его формой.
2. После оплаты Робокасса стучится на ResultURL (`/webhooks/robokassa/result`).
   Только это уведомление с верной подписью (пароль №2) проводит оплату.
3. Клиента Робокасса возвращает на SuccessURL или FailURL — это просто переходы
   браузера, оплату они не подтверждают.

Адреса ResultURL / SuccessURL / FailURL и алгоритм подписи задаются в кабинете
магазина, в «Технических настройках». Подпись считается тем же алгоритмом, что
указан там (ROBOKASSA_HASH_ALGO).

`operation_state` (нижняя часть модуля) — OpStateExt: состояние оплаты по номеру
счёта. Им сверка находит оплаты, уведомление о которых до нас не дошло. Тестовые
оплаты (IsTest=1) метод не видит.

Возвратов в системе нет: клиент пишет в поддержку, деньги владелица возвращает
в кабинете Робокассы.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

import httpx

from core.config import get_settings
from core.errors import ExternalServiceError, ValidationError
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
    test_amount = get_settings().payments_test_amount_kopecks
    if test_amount and amount_kopecks == test_amount != order.total_kopecks:
        # Режим проверки: настоящий чек на проверочную сумму одной строкой.
        items = [
            {
                "name": "Подарочный бокс (проверка оплаты)",
                "quantity": 1,
                "sum": float(Decimal(amount_kopecks) / 100),
                "payment_method": "full_payment",
                "payment_object": "commodity",
                "tax": "none",
            },
        ]
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


def checkout_fields(payment: Payment, order: Order) -> dict[str, str]:
    """Поля POST-формы оплаты для этой попытки. Форма уходит на `payment_url`.

    Чек в подписи участвует URL-кодированным, и в поле формы кладётся он же: браузер
    закодирует его ещё раз при отправке, Робокасса раскодирует один раз — и получит
    ровно то, что подписано. Так устроен и пример в документации Робокассы.
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

    fields: dict[str, str] = {
        "MerchantLogin": settings.login,
        "OutSum": out_sum,
        "InvId": inv_id,
        "Description": PAYMENT_DESCRIPTION,
        "SignatureValue": signature,
        "Culture": "ru",
        "Encoding": "utf-8",
    }
    if receipt_encoded:
        fields["Receipt"] = receipt_encoded
    if order.recipient_email:
        fields["Email"] = order.recipient_email
    if settings.is_test:
        fields["IsTest"] = "1"
    return fields


def _signature_matches(expected_source: str, received: str) -> bool:
    return hmac.compare_digest(_hash(expected_source).lower(), (received or "").strip().lower())


def check_result_signature(out_sum: str, inv_id: str, signature: str) -> bool:
    """Подпись уведомления ResultURL: OutSum:InvId:Пароль#2."""
    return _signature_matches(f"{out_sum}:{inv_id}:{_password2()}", signature)


def check_success_signature(out_sum: str, inv_id: str, signature: str) -> bool:
    """Подпись возврата клиента на SuccessURL: OutSum:InvId:Пароль#1."""
    return _signature_matches(f"{out_sum}:{inv_id}:{_password1()}", signature)


# --- обращения к Робокассе ---

#: Сколько ждём ответа Робокассы. Её методы отвечают за доли секунды; дольше — сбой.
_TIMEOUT = httpx.Timeout(15.0)

#: State.Code в ответе OpStateExt: платёж проведён, деньги зачислены.
STATE_PAID = 100


@dataclass(frozen=True, slots=True)
class OperationState:
    """Что Робокасса знает о счёте."""

    #: Код состояния: 100 — оплачено, 50 — деньги получены и зачисляются, 10 — отменено…
    code: int
    #: Сумма, которую заплатил клиент, — как её прислала Робокасса.
    inc_sum: str | None
    payment_method: str | None

    @property
    def is_paid(self) -> bool:
        return self.code == STATE_PAID


def op_state_signature(inv_id: int) -> str:
    """Подпись OpStateExt: MerchantLogin:InvoiceID:Пароль#2."""
    return _hash(f"{get_settings().robokassa.login}:{inv_id}:{_password2()}")


def parse_operation_state(body: str) -> OperationState | None:
    """Разобрать ответ OpStateExt. None — Робокасса такой операции не знает.

    Пространство имён в ответе есть не всегда, поэтому ищем по локальным именам.
    """
    try:
        root = ET.fromstring(body)
    except ET.ParseError as error:
        raise ExternalServiceError("Робокасса прислала непонятный ответ о платеже") from error

    result = _xml_int(root, "{*}Result/{*}Code")
    if result == 3:
        # «Операция не найдена»: клиент открыл кассу, но до оплаты не дошёл.
        return None
    if result != 0:
        description = _xml_text(root, "{*}Result/{*}Description") or f"код {result}"
        raise ExternalServiceError(f"Робокасса не отдала состояние платежа: {description}")

    code = _xml_int(root, "{*}State/{*}Code")
    if code is None:
        raise ExternalServiceError("В ответе Робокассы нет состояния платежа")
    return OperationState(
        code=code,
        inc_sum=_xml_text(root, "{*}Info/{*}IncSum"),
        payment_method=_xml_text(root, "{*}Info/{*}PaymentMethod/{*}Code"),
    )


def _xml_text(root: ET.Element, path: str) -> str | None:
    node = root.find(path)
    if node is None or node.text is None:
        return None
    return node.text.strip() or None


def _xml_int(root: ET.Element, path: str) -> int | None:
    value = _xml_text(root, path)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


async def operation_state(inv_id: int) -> OperationState | None:
    """Состояние счёта в Робокассе (OpStateExt)."""
    settings = get_settings().robokassa
    params = {
        "MerchantLogin": settings.login,
        "InvoiceID": str(inv_id),
        "Signature": op_state_signature(inv_id),
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.get(settings.op_state_url, params=params)
            response.raise_for_status()
    except httpx.HTTPError as error:
        raise ExternalServiceError("Робокасса не ответила на запрос о платеже") from error
    return parse_operation_state(response.text)
