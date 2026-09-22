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


def normalize_search(value: str) -> str:
    """Строка для поиска: нижний регистр, «ё» как «е», без знаков препинания.

    Делается в Python, а не функцией lower() в базе: у базы с локалью C lower()
    не трогает кириллицу, и «москва» не находит «Москва».
    """
    folded = value.casefold().replace("ё", "е")
    cleaned = "".join(character if character.isalnum() else " " for character in folded)
    return " ".join(cleaned.split())


#: Слова, которые люди пишут в адресе, но которых может не быть в справочнике ПВЗ.
ADDRESS_STOPWORDS = frozenset(
    {
        "г",
        "гор",
        "город",
        "ул",
        "улица",
        "д",
        "дом",
        "пр",
        "просп",
        "проспект",
        "пер",
        "переулок",
        "ш",
        "шоссе",
        "б",
        "бульв",
        "бульвар",
        "пл",
        "площадь",
        "наб",
        "набережная",
        "к",
        "корп",
        "корпус",
        "стр",
        "строение",
        "кв",
        "обл",
        "область",
        "район",
        "мкр",
        "микрорайон",
        "россия",
        "рф",
    },
)


def search_words(query: str) -> list[str]:
    """Значимые слова запроса.

    «Москва, ул. Берёзовая аллея 19к1» -> [москва, березовая, аллея, 19к1].
    """
    return [word for word in normalize_search(query).split() if word not in ADDRESS_STOPWORDS]
