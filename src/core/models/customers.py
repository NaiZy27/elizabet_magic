"""Клиенты.

Храним минимум: то, что нужно службе доставки и для связи. Геопозицию не сохраняем —
после выбора ПВЗ она не нужна.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.models.base import Base, IdMixin, TimestampMixin

if TYPE_CHECKING:
    from core.models.delivery import PickupPoint
    from core.models.orders import Order


class Customer(IdMixin, TimestampMixin, Base):
    __tablename__ = "customers"

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    username: Mapped[str | None] = mapped_column(String(64), default=None)
    full_name: Mapped[str | None] = mapped_column(String(256), default=None)
    phone: Mapped[str | None] = mapped_column(String(32), default=None)
    #: Нужен Робокассе для отправки чека.
    email: Mapped[str | None] = mapped_column(String(256), default=None)

    #: Чтобы предложить «использовать данные прошлого заказа».
    last_pickup_point_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("pickup_points.id", ondelete="RESTRICT"),
        default=None,
    )

    #: Согласие на обработку персональных данных (152-ФЗ), дано перед первым заказом.
    consent_accepted_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        default=None,
    )
    #: Клиент заблокировал бота — ставится по TelegramForbiddenError.
    bot_blocked_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        default=None,
    )

    last_pickup_point: Mapped[PickupPoint | None] = relationship()
    orders: Mapped[list[Order]] = relationship(
        back_populates="customer",
        order_by="Order.created_at.desc()",
    )

    @property
    def display_name(self) -> str:
        """Как звать клиента в админке, если имя не заполнено."""
        return self.full_name or (f"@{self.username}" if self.username else "Без имени")

    def __repr__(self) -> str:
        return f"<Customer tg={self.telegram_user_id}>"
