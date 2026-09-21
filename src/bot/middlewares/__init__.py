"""Middleware бота: сессия базы и карточка клиента для каждого апдейта."""

from bot.middlewares.context import ContextMiddleware

__all__ = ["ContextMiddleware"]
