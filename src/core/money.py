"""Деньги.

В базе и в расчётах — только целые копейки (int). Рубли существуют лишь на границе
с человеком: при вводе в админке и при показе. Ни float, ни Decimal в модели не попадают.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

#: Неразрывный пробел: «3 900 ₽» не должно переноситься по строке.
NBSP = " "

_MAX_RUBLES = Decimal("100000000")  # защита от опечаток вида 12000000000


def format_rubles(kopecks: int, *, with_currency: bool = True) -> str:
    """Копейки -> строка для интерфейса: 390000 -> «3 900 ₽», 39050 -> «390,50 ₽»."""
    sign = "−" if kopecks < 0 else ""
    whole, rest = divmod(abs(kopecks), 100)
    grouped = f"{whole:,}".replace(",", NBSP)
    amount = grouped if rest == 0 else f"{grouped},{rest:02d}"
    return f"{sign}{amount}{NBSP}₽" if with_currency else f"{sign}{amount}"


def parse_rubles(raw: str) -> int:
    """Ввод из формы админки -> копейки. «1200», «1200,50», «1 200.50» -> int.

    Бросает ValueError с текстом, который можно показать пользователю у поля.
    """
    text = raw.strip().replace(NBSP, "").replace(" ", "").replace("₽", "").replace(",", ".")
    if not text:
        raise ValueError("Укажите сумму")
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValueError("Сумма указана неверно. Пример: 1200 или 1200,50") from None
    # Decimal("nan") и Decimal("Infinity") разбираются без ошибки — отсекаем отдельно.
    if not value.is_finite():
        raise ValueError("Сумма указана неверно. Пример: 1200 или 1200,50")
    if value < 0:
        raise ValueError("Сумма не может быть отрицательной")
    if value >= _MAX_RUBLES:
        raise ValueError("Слишком большая сумма — проверьте, не лишний ли ноль")
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and -exponent > 2:
        raise ValueError("В сумме не больше двух знаков после запятой")
    return int(value.scaleb(2).to_integral_value())


def kopecks_to_amount_str(kopecks: int) -> str:
    """Сумма для внешних API: 390000 -> «3900.00».

    Робокасса подписывает ровно ту строку, что уходит в запросе, поэтому формат
    суммы задаётся одной функцией на весь проект.
    """
    whole, rest = divmod(kopecks, 100)
    return f"{whole}.{rest:02d}"
