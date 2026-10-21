"""Модели SQLAlchemy.

Все модели импортируются здесь: Alembic видит схему целиком только через этот модуль.
"""

from core.models.base import Base
from core.models.catalog import Addon, Color, Product, ProductVariant
from core.models.customers import Customer
from core.models.delivery import DeliveryProvider, PickupPoint, Shipment
from core.models.orders import (
    ORDER_NUMBER_PREFIX,
    Order,
    OrderAddon,
    OrderEvent,
    OrderItem,
    order_number_seq,
)
from core.models.payments import Payment, Receipt
from core.models.settings import AdminUser, MessageTemplate, Setting

__all__ = [
    "ORDER_NUMBER_PREFIX",
    "Addon",
    "AdminUser",
    "Base",
    "Color",
    "Customer",
    "DeliveryProvider",
    "MessageTemplate",
    "Order",
    "OrderAddon",
    "OrderEvent",
    "OrderItem",
    "Payment",
    "PickupPoint",
    "Product",
    "ProductVariant",
    "Receipt",
    "Setting",
    "Shipment",
    "order_number_seq",
]
