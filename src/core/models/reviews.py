"""Отзывы клиентов.

Клиент оставляет отзыв в боте после получения заказа. Владелица листает отзывы
в рабочем режиме бота и одной кнопкой публикует понравившиеся в канал.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.models.base import Base, IdMixin, TimestampMixin

if TYPE_CHECKING:
    from core.models.customers import Customer
    from core.models.orders import Order


class Review(IdMixin, TimestampMixin, Base):
    __tablename__ = "reviews"
    __table_args__ = (Index("ix_reviews_created_at", "created_at"),)

    customer_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("customers.id", ondelete="CASCADE"),
        index=True,
    )
    #: К какому заказу отзыв. Пусто — отзыв оставлен не из карточки заказа.
    order_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("orders.id", ondelete="SET NULL"),
        default=None,
    )
    text: Mapped[str] = mapped_column(Text, default="", server_default="")
    #: Фото из Telegram: храним только file_id, сама картинка остаётся у Telegram.
    photo_file_id: Mapped[str | None] = mapped_column(String(256), default=None)
    #: Владелица убрала отзыв из списка (спам, случайное сообщение).
    hidden_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    #: Когда отзыв ушёл в канал — чтобы не опубликовать его дважды по ошибке.
    posted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    customer: Mapped[Customer] = relationship()
    order: Mapped[Order | None] = relationship()

    def __repr__(self) -> str:
        return f"<Review {self.id} customer={self.customer_id}>"
