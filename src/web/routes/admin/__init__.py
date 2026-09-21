"""Маршруты админки.

Страницы живут под /admin, запросы доски — под /api: так они по-разному ведут себя
без сессии (страница уводит на вход, запрос отвечает 401).
"""

from fastapi import APIRouter

from web.routes.admin import (
    auth,
    board,
    catalog,
    customers,
    delivery,
    orders,
    settings,
    stats,
)

router = APIRouter()

admin = APIRouter(prefix="/admin")
admin.include_router(auth.router)
admin.include_router(board.router)
admin.include_router(orders.router)
admin.include_router(catalog.router)
admin.include_router(delivery.router)
admin.include_router(customers.router)
admin.include_router(stats.router)
admin.include_router(settings.router)

router.include_router(admin)
router.include_router(board.api_router, prefix="/api")

__all__ = ["router"]
