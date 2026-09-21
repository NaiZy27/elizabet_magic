"""Подключения к Redis.

У каждой задачи своя база: кэш ответов служб доставки, счётчики попыток входа,
FSM бота и очередь задач не мешают друг другу.
"""

from __future__ import annotations

from redis.asyncio import Redis

from core.config import get_settings

#: Открытые подключения по адресу — чтобы закрыть их при остановке процесса.
_clients: dict[str, Redis] = {}


def _client(url: str) -> Redis:
    client = _clients.get(url)
    if client is None:
        client = Redis.from_url(url, decode_responses=True)
        _clients[url] = client
    return client


def cache_redis() -> Redis:
    """Кэш токенов и расчётов доставки."""
    return _client(get_settings().redis_cache_url)


def limits_redis() -> Redis:
    """Счётчики попыток входа в админку."""
    return _client(get_settings().redis_limits_url)


async def close_redis() -> None:
    """Закрыть все подключения при остановке процесса."""
    for client in list(_clients.values()):
        await client.aclose()
    _clients.clear()
