"""Обработчики бота.

Порядок подключения важен: сценарий оформления перехватывает ввод на своих шагах,
поэтому он идёт раньше общего меню, а «запасной» обработчик — последним.
"""

from aiogram import Router

from bot.handlers import checkout, fallback, menu, my_orders


def build_router() -> Router:
    router = Router(name="root")
    router.include_router(checkout.router)
    router.include_router(my_orders.router)
    router.include_router(menu.router)
    router.include_router(fallback.router)
    return router


__all__ = ["build_router"]
