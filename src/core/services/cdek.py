"""СДЭК API v2: токен, тариф, пункты, заказы и вебхуки.

Только обращения к СДЭК и перевод его ответов в наши понятия. Что делать с заказом
у нас (этапы, уведомления) — в core.services.shipments.

Проверено на тестовой среде api.edu.cdek.ru (10.2026):
- тариф 136 «Посылка склад-склад» работает с типом заказа 1 («интернет-магазин»);
  клиенту называем `total_sum` — с НДС и страховкой объявленной стоимости;
- заказ создаётся асинхронно: POST /orders сразу отвечает uuid и ACCEPTED, номер
  СДЭК (`cdek_number`) появляется в GET /orders/{uuid} через пару секунд, либо запрос
  уходит в INVALID с ошибками;
- вебхуки не подписаны, поэтому по уведомлению мы перезапрашиваем заказ сами.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

import httpx

from core.cache import cache_redis
from core.config import get_settings
from core.enums import ShipmentStatus
from core.errors import ExternalServiceError

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(20.0)

#: Тип заказа «интернет-магазин»: в нём доступен тариф 136.
ORDER_TYPE_SHOP = 1

#: Сколько хранить посчитанный тариф: цены СДЭК меняются редко, а пересчёт идёт на
#: каждое изменение корзины.
TARIFF_CACHE_SECONDS = 6 * 3600
#: Город пункта меняется ещё реже.
POINT_CACHE_SECONDS = 24 * 3600

#: Статусы СДЭК -> наш этап отправления. Названия у СДЭК в разных версиях
#: документации расходятся, поэтому учтены оба варианта. Всё, чего здесь нет, —
#: промежуточные этапы пути (сортировка, транзит), то есть «в пути».
_CREATED = {"CREATED", "ACCEPTED"}
_ARRIVED = {
    "ACCEPTED_AT_PICK_UP_POINT",
    "ACCEPTED_AT_WAREHOUSE_ON_DEMAND",
    "POSTOMAT_POSTED",
}
_DELIVERED = {"DELIVERED", "POSTOMAT_RECEIVED"}
_RETURNED = {"NOT_DELIVERED", "RETURNED"}
_CANCELLED = {"REMOVED", "INVALID"}


def map_status(code: str | None) -> ShipmentStatus:
    """Код статуса СДЭК -> наш нормализованный статус отправления."""
    code = (code or "").upper()
    if not code or code in _CREATED:
        return ShipmentStatus.CREATED
    if code in _ARRIVED:
        return ShipmentStatus.ARRIVED
    if code in _DELIVERED:
        return ShipmentStatus.DELIVERED
    if code in _RETURNED:
        return ShipmentStatus.RETURNED
    if code in _CANCELLED:
        return ShipmentStatus.CANCELLED
    return ShipmentStatus.IN_TRANSIT


@dataclass(frozen=True, slots=True)
class Tariff:
    price_kopecks: int
    days_min: int | None
    days_max: int | None


@dataclass(frozen=True, slots=True)
class PointInfo:
    code: str
    city_code: int
    city: str
    address: str


@dataclass(frozen=True, slots=True)
class OrderInfo:
    uuid: str
    cdek_number: str | None
    #: Последний статус: код и название по-русски.
    status_code: str | None
    status_name: str | None
    #: Состояние запроса на создание: ACCEPTED, WAITING, SUCCESSFUL, INVALID.
    create_state: str | None
    errors: tuple[str, ...]


# --- транспорт ---


def _settings():
    return get_settings().cdek


def _key(*parts: object) -> str:
    """Ключ кэша: тестовая и боевая среды СДЭК не делят записи."""
    base = hashlib.sha1(_settings().base_url.encode()).hexdigest()[:8]
    return "cdek:" + base + ":" + ":".join(str(part) for part in parts)


async def _token(*, fresh: bool = False) -> str:
    """OAuth-токен. Живёт час — держим в Redis, чтобы не просить его на каждый запрос."""
    settings = _settings()
    key = _key("token", settings.client_id)
    redis = cache_redis()
    if not fresh:
        cached = await redis.get(key)
        if cached:
            return cached
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                f"{settings.base_url}/oauth/token",
                params={
                    "grant_type": "client_credentials",
                    "client_id": settings.client_id,
                    "client_secret": settings.client_secret.get_secret_value(),
                },
            )
    except httpx.HTTPError as error:
        raise ExternalServiceError("СДЭК не ответил на запрос авторизации") from error
    if response.status_code != httpx.codes.OK:
        raise ExternalServiceError("СДЭК не принял ключи API — проверьте CDEK_CLIENT_ID/SECRET")
    data = response.json()
    token = str(data["access_token"])
    ttl = max(60, int(data.get("expires_in", 3600)) - 120)
    await redis.set(key, token, ex=ttl)
    return token


def _errors(data: Any) -> list[str]:
    """Тексты ошибок из ответа СДЭК — и общих, и по запросам."""
    if not isinstance(data, dict):
        return []
    found = [str(error.get("message") or error.get("code")) for error in data.get("errors") or []]
    for request in data.get("requests") or []:
        found += [
            str(error.get("message") or error.get("code")) for error in request.get("errors") or []
        ]
    return found


async def _request(method: str, path: str, **kwargs: Any) -> Any:
    """Запрос с токеном. Протухший токен (401) обновляется один раз."""
    url = f"{_settings().base_url}{path}"
    for attempt in range(2):
        token = await _token(fresh=attempt > 0)
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.request(
                    method,
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    **kwargs,
                )
        except httpx.HTTPError as error:
            raise ExternalServiceError("СДЭК не ответил") from error
        if response.status_code == httpx.codes.UNAUTHORIZED and attempt == 0:
            continue
        try:
            data = response.json() if response.content else None
        except ValueError:
            data = None
        if response.is_success:
            return data
        message = "; ".join(_errors(data)) or f"HTTP {response.status_code}"
        raise ExternalServiceError(f"СДЭК: {message}")
    raise ExternalServiceError("СДЭК не принял токен")


# --- справочники и тариф ---


async def point_info(code: str) -> PointInfo:
    """Город и адрес пункта СДЭК по его коду. Нужны калькулятору и сообщению владелице."""
    key = _key("point", code)
    redis = cache_redis()
    cached = await redis.get(key)
    if cached:
        return PointInfo(**json.loads(cached))

    points = await _request("GET", "/deliverypoints", params={"code": code})
    if not points:
        raise ExternalServiceError(f"СДЭК не знает пункт {code}")
    location = points[0].get("location") or {}
    info = PointInfo(
        code=code,
        city_code=int(location["city_code"]),
        city=str(location.get("city") or ""),
        address=str(location.get("address") or location.get("address_full") or ""),
    )
    await redis.set(key, json.dumps(asdict(info), ensure_ascii=False), ex=POINT_CACHE_SECONDS)
    return info


def _packages(weight_g: int, length_cm: int, width_cm: int, height_cm: int) -> list[dict]:
    return [
        {
            "weight": max(1, weight_g),
            "length": max(1, length_cm),
            "width": max(1, width_cm),
            "height": max(1, height_cm),
        },
    ]


def _kopecks_rounded_up(rubles: Any) -> int:
    """Цену СДЭК (234.6) округляем вверх до рубля: клиенту — круглая сумма."""
    return math.ceil(Decimal(str(rubles))) * 100


async def calculate(
    *,
    to_point: str,
    weight_g: int,
    length_cm: int,
    width_cm: int,
    height_cm: int,
    declared_kopecks: int,
) -> Tariff:
    """Стоимость доставки от пункта сдачи владелицы до пункта клиента по тарифу 136."""
    settings = _settings()
    declared_rubles = max(0, declared_kopecks) // 100
    key = _key(
        "tariff",
        settings.tariff_code,
        settings.shipment_point,
        to_point,
        weight_g,
        length_cm,
        width_cm,
        height_cm,
        declared_rubles,
    )
    redis = cache_redis()
    cached = await redis.get(key)
    if cached:
        return Tariff(**json.loads(cached))

    source = await point_info(settings.shipment_point)
    target = await point_info(to_point)
    body: dict[str, Any] = {
        "type": ORDER_TYPE_SHOP,
        "tariff_code": settings.tariff_code,
        "from_location": {"code": source.city_code},
        "to_location": {"code": target.city_code},
        "packages": _packages(weight_g, length_cm, width_cm, height_cm),
    }
    if declared_rubles:
        # Страховка объявленной стоимости — та же, что попадёт в заказ.
        body["services"] = [{"code": "INSURANCE", "parameter": str(declared_rubles)}]
    data = await _request("POST", "/calculator/tariff", json=body)
    if not isinstance(data, dict) or data.get("total_sum") is None:
        raise ExternalServiceError(
            "СДЭК не посчитал доставку: " + ("; ".join(_errors(data)) or "нет тарифа")
        )
    tariff = Tariff(
        price_kopecks=_kopecks_rounded_up(data["total_sum"]),
        days_min=data.get("period_min"),
        days_max=data.get("period_max"),
    )
    await redis.set(key, json.dumps(asdict(tariff)), ex=TARIFF_CACHE_SECONDS)
    return tariff


# --- заказы ---


async def create_order(payload: dict[str, Any]) -> str:
    """Отправить заказ в СДЭК. Возвращает uuid; номер СДЭК появится позже."""
    data = await _request("POST", "/orders", json=payload)
    uuid = ((data or {}).get("entity") or {}).get("uuid")
    if not uuid:
        raise ExternalServiceError(
            "СДЭК не принял заказ: " + ("; ".join(_errors(data)) or "нет uuid в ответе")
        )
    return str(uuid)


def parse_order(data: Any) -> OrderInfo:
    """Ответ GET /orders/{uuid} -> то, что нам нужно."""
    entity = (data or {}).get("entity") or {}
    statuses = sorted(entity.get("statuses") or [], key=lambda item: item.get("date_time") or "")
    last = statuses[-1] if statuses else {}
    create = next(
        (
            request
            for request in (data or {}).get("requests") or []
            if request.get("type") == "CREATE"
        ),
        {},
    )
    return OrderInfo(
        uuid=str(entity.get("uuid") or ""),
        cdek_number=str(entity["cdek_number"]) if entity.get("cdek_number") else None,
        status_code=last.get("code"),
        status_name=last.get("name"),
        create_state=create.get("state"),
        errors=tuple(_errors(data)),
    )


async def get_order(uuid: str) -> OrderInfo:
    return parse_order(await _request("GET", f"/orders/{uuid}"))


# --- вебхуки ---


def webhook_url() -> str | None:
    """Адрес, на который СДЭК шлёт статусы. None — если принимать их некуда.

    На тестовой среде не подписываемся: её ключи общие на всех, и к нам шли бы
    статусы чужих тестовых заказов. Там статусы приходят сверкой по расписанию.
    """
    settings = get_settings()
    if settings.cdek.is_test:
        return None
    secret = settings.cdek.webhook_secret.get_secret_value()
    base = settings.public_base_url.rstrip("/")
    if not secret or not base.startswith("https://"):
        return None
    return f"{base}/webhooks/cdek/{secret}"


async def ensure_webhook() -> bool:
    """Подписаться на статусы заказов, если подписки ещё нет. True — подписались сейчас."""
    url = webhook_url()
    if url is None:
        return False
    existing = await _request("GET", "/webhooks") or []
    if any(item.get("url") == url and item.get("type") == "ORDER_STATUS" for item in existing):
        return False
    await _request("POST", "/webhooks", json={"type": "ORDER_STATUS", "url": url})
    return True
