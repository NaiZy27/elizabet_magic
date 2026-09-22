"""Оплата и чеки. Одна запись платежа — одна попытка оплаты."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.enums import (
    PaymentProvider,
    PaymentStatus,
    ProviderEnvironment,
    ReceiptProvider,
    ReceiptStatus,
)
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
    refunded_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        default=None,
    )

    order: Mapped[Order] = relationship(back_populates="payments")
    receipt: Mapped[Receipt | None] = relationship(back_populates="payment", uselist=False)

    def __repr__(self) -> str:
        return f"<Payment {self.id} order={self.order_id} {self.status}>"


class Receipt(IdMixin, TimestampMixin, Base):
    """Чек самозанятого по оплате.

    Хранится бессрочно: это подтверждение, что продажа передана в «Мой налог».
    Возврат в НПД оформляется аннулированием чека, поэтому отдельной записи
    «чек возврата» нет — у того же чека меняется статус.
    """

    __tablename__ = "receipts"
    __table_args__ = (
        non_negative("amount_kopecks"),
        # На одну оплату — один чек продажи.
        UniqueConstraint("payment_id", name="uq_receipts_payment"),
        Index("ix_receipts_order", "order_id"),
    )

    order_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("orders.id", ondelete="RESTRICT"),
    )
    payment_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("payments.id", ondelete="RESTRICT"),
    )
    provider: Mapped[ReceiptProvider] = mapped_column(
        enum_type(ReceiptProvider, "receipt_provider"),
    )
    status: Mapped[ReceiptStatus] = mapped_column(
        enum_type(ReceiptStatus, "receipt_status"),
        default=ReceiptStatus.PENDING,
    )
    amount_kopecks: Mapped[int] = money()
    #: Куда отправлен чек — email покупателя на момент оплаты.
    buyer_email: Mapped[str | None] = mapped_column(String(256), default=None)
    #: Что написано в чеке: [{"name": …, "amount_kopecks": …}].
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default="[]")

    #: Номер чека в «Мой налог».
    external_id: Mapped[str | None] = mapped_column(String(128), default=None)
    #: Ссылка на печатную форму чека на lknpd.nalog.ru.
    url: Mapped[str | None] = mapped_column(String(512), default=None)
    registered_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        default=None,
    )
    annulled_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        default=None,
    )
    annul_reason: Mapped[str | None] = mapped_column(Text, default=None)
    #: Ответ сервиса как есть — для разбора спорных случаев.
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)

    order: Mapped[Order] = relationship(back_populates="receipts")
    payment: Mapped[Payment] = relationship(back_populates="receipt")

    def __repr__(self) -> str:
        return f"<Receipt {self.id} payment={self.payment_id} {self.status}>"
