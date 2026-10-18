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


def phone_e164(value: str | None) -> str | None:
    """Российский номер в виде +79001234567. None — если это не похоже на номер.

    Клиент пишет как угодно: «8 900 123-45-67», «+7(900)1234567», «9001234567».
    """
    digits = "".join(character for character in value or "" if character.isdigit())
    if len(digits) == 10:
        digits = f"7{digits}"
    elif len(digits) == 11 and digits[0] == "8":
        digits = f"7{digits[1:]}"
    if len(digits) != 11 or digits[0] != "7":
        return None
    return f"+{digits}"


def phone_pretty(value: str | None) -> str:
    """«+7 900 123-45-67» — чтобы владелице было удобно читать и переписывать."""
    e164 = phone_e164(value)
    if e164 is None:
        return (value or "").strip()
    d = e164[2:]
    return f"+7 {d[:3]} {d[3:6]}-{d[6:8]}-{d[8:]}"


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
