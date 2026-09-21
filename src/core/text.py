"""Мелочи русского текста, нужные и боту, и админке."""

from __future__ import annotations


def pluralize(count: int, one: str, few: str, many: str) -> str:
    """Форма слова по числу: 1 ложечка, 3 ложечки, 5 ложечек."""
    n = abs(count) % 100
    if 11 <= n <= 14:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def spoons_phrase(count: int) -> str:
    """«3 ложечки»."""
    return f"{count} {pluralize(count, 'ложечка', 'ложечки', 'ложечек')}"


def days_phrase(count: int) -> str:
    """«2 дня»."""
    return f"{count} {pluralize(count, 'день', 'дня', 'дней')}"


def orders_phrase(count: int) -> str:
    """«5 заказов»."""
    return f"{count} {pluralize(count, 'заказ', 'заказа', 'заказов')}"


def truncate(value: str, limit: int, tail: str = "…") -> str:
    """Обрезать длинный текст для карточки или кнопки."""
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - len(tail))].rstrip() + tail
