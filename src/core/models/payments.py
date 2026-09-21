"""Оплата. Одна запись — одна попытка оплаты."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.enums import PaymentProvider, PaymentStatus, ProviderEnvironment
from core.models.base import Base, IdMixin, TimestampMixin, enum_type, money, non_negative

if TYPE_CHECKING:
    from core.models.orders import Order


class Payment(IdMixin, TimestampMixin, Base):
    """Попытка оплаты.

    `id` — он же InvId для Робокассы, поэтому отдельной колонки под него нет:
    номер счёта уникален на попытку, а не на заказ.
    """

    __tablename__ = "payments"
    __table_args__ = (
        non_negative("amount_kopecks"),
        non_negative("fee_kopecks"),
        UniqueConstraint("provider", "environment", "external_id"),
        # Одна активная попытка на заказ: клиент не сможет открыть две ссылки на оплату.
        Index(
            "uq_payments_pending_order",
            "order_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        # И не больше одной успешной оплаты: повторный ResultURL не создаст вторую.
        Index(
            "uq_payments_paid_order",
            "order_id",
            unique=True,
            postgresql_where=text("status = 'paid'"),
        ),
    )

    order_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("orders.id", ondelete="RESTRICT"),
        index=True,
    )
    provider: Mapped[PaymentProvider] = mapped_column(
        enum_type(PaymentProvider, "payment_provider"),
        default=PaymentProvider.ROBOKASSA,
    )
    environment: Mapped[ProviderEnvironment] = mapped_column(
        enum_type(ProviderEnvironment, "payment_environment"),
        default=ProviderEnvironment.TEST,
    )
    #: Идентификатор операции у провайдера, если он его выдаёт.
    external_id: Mapped[str | None] = mapped_column(String(128), default=None)

    #: Для ссылки /pay/<token> — чтобы в адресе не светился id платежа.
    public_token: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        unique=True,
        default=uuid.uuid4,
    )

    amount_kopecks: Mapped[int] = money()
    status: Mapped[PaymentStatus] = mapped_column(
        enum_type(PaymentStatus, "payment_status"),
        default=PaymentStatus.PENDING,
    )
    #: Тело уведомления от провайдера без подписи — для разбора спорных случаев.
    raw_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    #: Комиссия, если провайдер её сообщает.
    fee_kopecks: Mapped[int | None] = money(nullable=True, default=None)
    paid_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    order: Mapped[Order] = relationship(back_populates="payments")

    def __repr__(self) -> str:
        return f"<Payment {self.id} order={self.order_id} {self.status}>"
