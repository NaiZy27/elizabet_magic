"""Ошибки предметной области.

Текст ошибки написан по-русски и рассчитан на показ человеку: бот отправляет его
клиенту, админка — выводит у поля или тостом.
"""

from __future__ import annotations


class DomainError(Exception):
    """Нарушено бизнес-правило."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(DomainError):
    """Запись не найдена или недоступна."""


class ValidationError(DomainError):
    """Некорректные данные от пользователя."""


class ConflictError(DomainError):
    """Состояние изменилось параллельно: доска устарела, заказ уже оплачен и т. п."""


class ExternalServiceError(DomainError):
    """Внешний сервис недоступен или ответил ошибкой."""
