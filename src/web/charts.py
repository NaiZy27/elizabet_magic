"""Геометрия графиков для шаблонов: столбики, кольца, линии тренда.

Графики рисуются обычным SVG прямо в шаблоне — без внешних библиотек и CDN.
Здесь только арифметика: шаблон получает готовые координаты.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

#: Палитра графиков: розовые и тёплые оттенки, различимые на тёмном фоне.
PALETTE = ("#ec5f9f", "#f6a8c9", "#a98be0", "#f2a57a", "#6fc6a9", "#7fa7e8", "#d9c06b")


def nice_ceiling(value: float, headroom: float = 1.15) -> float:
    """Верх шкалы: максимум с запасом, округлённый до «красивого» числа (1, 1.5, 2, 2.5, 3, 4, 5, 6, 8 × 10ⁿ).

    Без запаса самый высокий столбик всегда упирается в потолок, и при ровных продажах
    график превращается в сплошную стену одинаковых столбиков.
    """
    if value <= 0:
        return 1
    target = value * headroom
    magnitude = 10 ** math.floor(math.log10(target))
    for step in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if step * magnitude >= target:
            return step * magnitude
    return 10 * magnitude


def bars(
    values: Sequence[float],
    *,
    height: float = 120,
    gap: float = 0.38,
    top: float | None = None,
) -> list[dict[str, Any]]:
    """Столбики в координатах 0..100 по ширине и 0..height по высоте.

    Высота считается от «красивого» верха шкалы (nice_ceiling), а не от максимума.
    """
    count = len(values)
    if not count:
        return []
    top = top or nice_ceiling(max(values))
    peak = max(values)
    slot = 100 / count
    width = slot * (1 - gap)
    result = []
    for index, value in enumerate(values):
        h = 0 if value <= 0 else max(2.0, value / top * height)
        result.append(
            {
                "x": round(index * slot + (slot - width) / 2, 3),
                "w": round(width, 3),
                "y": round(height - h, 2),
                "h": round(h, 2),
                "value": value,
                "is_max": value == peak and value > 0,
            },
        )
    return result


def donut(
    parts: Sequence[tuple[str, float]],
    *,
    radius: float = 42,
    colors: Sequence[str] = PALETTE,
) -> list[dict[str, Any]]:
    """Сегменты кольца для <circle> со stroke-dasharray. Длина окружности — в единицах SVG."""
    total = sum(value for _label, value in parts if value > 0)
    circumference = 2 * math.pi * radius
    result = []
    offset = 0.0
    for index, (label, value) in enumerate(parts):
        share = value / total if total else 0
        length = circumference * share
        result.append(
            {
                "label": label,
                "value": value,
                "percent": round(share * 100),
                "color": colors[index % len(colors)],
                "dash": f"{max(0.0, length - 1.5):.2f} {circumference:.2f}",
                "offset": f"{-offset:.2f}",
                "length": round(length, 2),
            },
        )
        offset += length
    return result


def sparkline(values: Sequence[float], *, width: float = 120, height: float = 36) -> dict[str, str]:
    """Линия тренда и залитая область под ней."""
    if len(values) < 2:
        return {"line": "", "area": ""}
    top = max(values) or 1
    step = width / (len(values) - 1)
    points = [(i * step, height - (v / top) * (height - 4) - 2) for i, v in enumerate(values)]
    line = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in points)
    area = f"{line} L{width:.1f},{height:.1f} L0,{height:.1f} Z"
    return {"line": line, "area": area}


def percent(part: float, whole: float) -> int:
    return round(part * 100 / whole) if whole else 0
