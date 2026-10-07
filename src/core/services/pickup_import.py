"""Загрузка справочника пунктов выдачи из файла в кэш `pickup_points`.

Пока службы доставки не подключены по API, справочник собирается снаружи
(СДЭК — публичный список офисов, Яндекс — агрегатор) и заливается этим модулем:

    python -m core.services.pickup_import /tmp/pickup_points.jsonl --environment production

Файл — JSON Lines, по строке на пункт:
    {"provider": "cdek", "external_id": "MSK2567", "name": "...", "city": "Москва",
     "city_code": "44", "address": "...", "latitude": 55.87, "longitude": 37.42,
     "working_hours": "Пн-Пт 10:00-21:00", "max_weight_g": 80000}

Точки сопоставляются по (служба, контур, external_id): новые добавляются, известные
обновляются. Точки службы, которых нет в файле, выключаются, а не удаляются —
на них могут ссылаться заказы.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from pathlib import Path

from sqlalchemy import select

from core.clock import now_utc
from core.db import dispose_engine, session_scope
from core.enums import DeliveryProviderCode, ProviderEnvironment
from core.models import DeliveryProvider, PickupPoint

_FIELDS = (
    "name",
    "city",
    "city_code",
    "address",
    "latitude",
    "longitude",
    "working_hours",
    "max_weight_g",
)


def _read(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as file:
        for line in file:
            if line.strip():
                rows.append(json.loads(line))
    return rows


async def import_points(path: Path, environment: ProviderEnvironment) -> Counter:
    rows = _read(path)
    providers = {DeliveryProviderCode(row["provider"]) for row in rows}
    stats: Counter = Counter()
    synced_at = now_utc()

    async with session_scope() as session:
        existing = {
            (point.provider, point.external_id): point
            for point in await session.scalars(
                select(PickupPoint).where(
                    PickupPoint.environment == environment,
                    PickupPoint.provider.in_(providers),
                ),
            )
        }
        seen: set[tuple[str, str]] = set()

        for row in rows:
            key = (DeliveryProviderCode(row["provider"]), str(row["external_id"]))
            if key in seen:
                continue
            seen.add(key)
            values = {field: row.get(field) for field in _FIELDS}
            values["name"] = (values["name"] or values["address"])[:256]
            values["address"] = values["address"][:512]

            point = existing.get(key)
            if point is None:
                session.add(
                    PickupPoint(
                        provider=key[0],
                        environment=environment,
                        external_id=key[1],
                        is_active=True,
                        synced_at=synced_at,
                        **values,
                    ),
                )
                stats["добавлено"] += 1
            else:
                for field, value in values.items():
                    setattr(point, field, value)
                point.is_active = True
                point.synced_at = synced_at
                stats["обновлено"] += 1

        for key, point in existing.items():
            if key not in seen and point.is_active:
                point.is_active = False
                stats["выключено"] += 1

        for provider in await session.scalars(
            select(DeliveryProvider).where(DeliveryProvider.code.in_(providers)),
        ):
            provider.pickup_points_synced_at = synced_at
            provider.last_sync_error = None

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Загрузить пункты выдачи из JSONL")
    parser.add_argument("path", type=Path)
    parser.add_argument(
        "--environment",
        choices=[item.value for item in ProviderEnvironment],
        default=ProviderEnvironment.PRODUCTION.value,
    )
    args = parser.parse_args()

    async def run() -> None:
        try:
            stats = await import_points(args.path, ProviderEnvironment(args.environment))
        finally:
            await dispose_engine()
        summary = ", ".join(f"{name}: {count}" for name, count in stats.items())
        print(summary or "нечего загружать")  # noqa: T201 — консольная команда

    asyncio.run(run())


if __name__ == "__main__":
    main()
