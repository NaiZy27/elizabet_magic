"""Исходящие сообщения: очередь на отправку, которая живёт в базе.

Сообщение записывается в той же транзакции, что и изменение заказа, поэтому оно
не теряется ни при откате (его просто нет), ни при сбое Redis или перезапуске
воркера (оно лежит в базе и уйдёт при следующем проходе обработчика).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import DateTime, Index, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.enums import OutboxKind, OutboxStatus
from core.models.base import Base, IdMixin, TimestampMixin, enum_type


class OutboxMessage(IdMixin, TimestampMixin, Base):
    """Одно сообщение, которое нужно отправить в Telegram."""

    __tablename__ = "outbox"
    __table_args__ = (
        # Обработчик раз в минуту берёт то, чему подошло время.
        Index(
            "ix_outbox_due",
            "next_attempt_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    kind: Mapped[OutboxKind] = mapped_column(enum_type(OutboxKind, "outbox_kind"))
    #: Что отправлять: id заказа, ключ шаблона, текст — смотря по виду сообщения.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    status: Mapped[OutboxStatus] = mapped_column(
        enum_type(OutboxStatus, "outbox_status"),
        default=OutboxStatus.PENDING,
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    #: Когда пробовать в следующий раз. Пока сообщение отправляется — «аренда»: если
    #: воркер упадёт посреди отправки, сообщение возьмут снова, когда она истечёт.
    next_attempt_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)

    def __repr__(self) -> str:
        return f"<OutboxMessage {self.id} {self.kind} {self.status}>"
