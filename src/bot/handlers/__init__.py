"""Обработчики бота.

Порядок подключения важен: рабочий режим владелицы идёт первым и забирает её
сообщения себе, дальше сценарий оформления перехватывает ввод на своих шагах,
затем общее меню, а «запасной» обработчик — последним.
"""

from aiogram import Router

from bot.handlers import checkout, fallback, menu, my_orders, owner


def build_router() -> Router:
    router = Router(name="root")
    router.include_router(owner.router)
    router.include_router(checkout.router)
    router.include_router(my_orders.router)
    router.include_router(menu.router)
    router.include_router(fallback.router)
    return router


__all__ = ["build_router"]
