"""Деньги и русский текст: форматы, которые видит клиент, и разбор ввода из админки."""

from __future__ import annotations

import pytest

from core.money import NBSP, format_rubles, kopecks_to_amount_str, parse_rubles
from core.text import days_phrase, pluralize, spoons_phrase, truncate


def test_format_whole_rubles():
    assert format_rubles(390000) == f"3{NBSP}900{NBSP}₽"
    assert format_rubles(50000) == f"500{NBSP}₽"
    assert format_rubles(0) == f"0{NBSP}₽"


def test_format_keeps_kopecks_when_they_matter():
    assert format_rubles(120050) == f"1{NBSP}200,50{NBSP}₽"
    assert format_rubles(5) == f"0,05{NBSP}₽"


def test_format_without_currency():
    assert format_rubles(390000, with_currency=False) == f"3{NBSP}900"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1200", 120000),
        ("1200,50", 120050),
        ("1200.50", 120050),
        (f"1{NBSP}200,50", 120050),
        ("1 200", 120000),
        (" 900 ₽ ", 90000),
        ("0", 0),
    ],
)
def test_parse_rubles(raw, expected):
    assert parse_rubles(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "бесплатно", "-100", "1200,555", "nan", "Infinity", "999999999999"],
)
def test_parse_rubles_rejects_garbage(raw):
    with pytest.raises(ValueError):
        parse_rubles(raw)


def test_amount_for_external_api():
    # Робокасса подписывает ровно эту строку — формат фиксированный.
    assert kopecks_to_amount_str(390000) == "3900.00"
    assert kopecks_to_amount_str(120050) == "1200.50"
    assert kopecks_to_amount_str(0) == "0.00"


@pytest.mark.parametrize(
    ("count", "expected"),
    [(1, "ложечка"), (2, "ложечки"), (4, "ложечки"), (5, "ложечек"), (11, "ложечек"), (21, "ложечка")],
)
def test_pluralize(count, expected):
    assert pluralize(count, "ложечка", "ложечки", "ложечек") == expected


def test_phrases():
    assert spoons_phrase(3) == "3 ложечки"
    assert days_phrase(1) == "1 день"
    assert days_phrase(5) == "5 дней"


def test_truncate():
    assert truncate("Короткий", 20) == "Короткий"
    assert truncate("Очень длинное название бокса", 12) == "Очень длинн…"


def test_normalize_search_folds_case_and_yo():
    from core.text import normalize_search

    assert normalize_search("г. Москва, ул. Берёзовая аллея, 19к1") == (
        "г москва ул березовая аллея 19к1"
    )


def test_search_words_drop_address_noise():
    from core.text import search_words

    assert search_words("москва, ул березовая аллея 19к1") == [
        "москва",
        "березовая",
        "аллея",
        "19к1",
    ]
    assert search_words("г. Казань, д. 5") == ["казань", "5"]
    assert search_words("  ,, ") == []
