"""Робокасса: сверка оплат и чеки Робочеков. Возвраты убраны из системы.

- payments: отметка «клиента отправили в кассу» (по ней работает сверка) и «состав
  чека ушёл в Робочеки»;
- receipts.status: «sent» — чек передан Робочекам, они регистрируют его сами;
- возвраты теперь вне системы — клиент пишет в поддержку, деньги владелица
  возвращает в кабинете Робокассы и аннулирует чек в «Мой налог». Поэтому уходят
  отметки о возврате и аннулировании (orders/payments.refunded_at, receipts.annulled_at,
  receipts.annul_reason), статусы «refunded» / «annulled», причина отмены «refund»,
  события «refunded» / «receipt_annulled» и шаблон «Оплата возвращена».

Записей с возвратами в базе нет — ограничения сужаются без переноса данных.

Revision ID: 0007_robokassa_reconcile
Revises: 0006_catalog_archive
Create Date: 2026-10-09 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_robokassa_reconcile"
down_revision: str | None = "0006_catalog_archive"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_PAYMENT_STATUSES = ("pending", "paid", "failed", "cancelled", "refunded")
PAYMENT_STATUSES = ("pending", "paid", "failed", "cancelled")

OLD_RECEIPT_STATUSES = ("pending", "registered", "annulled", "failed")
RECEIPT_STATUSES = ("pending", "sent", "registered", "failed")

OLD_CANCEL_REASONS = ("expired", "customer", "admin", "refund")
CANCEL_REASONS = ("expired", "customer", "admin")

ORDER_EVENT_TYPES = (
    "created",
    "submitted",
    "payment_created",
    "payment_succeeded",
    "payment_failed",
    "status_changed",
    "queue_reordered",
    "production_days_changed",
    "promised_date_changed",
    "admin_note_changed",
    "shipment_created",
    "track_assigned",
    "shipment_status_changed",
    "delivery_price_fallback",
    "expired",
    "notification_sent",
    "notification_failed",
    "revived",
    "payment_after_cancel",
    "receipt_registered",
    "arrived_at_pickup",
)
OLD_ORDER_EVENT_TYPES = (*ORDER_EVENT_TYPES, "receipt_annulled", "refunded")

#: (имя ограничения, таблица, колонка)
PAYMENT_STATUS_CHECK = ("ck_payments_payment_status", "payments", "status")
RECEIPT_STATUS_CHECK = ("ck_receipts_receipt_status", "receipts", "status")
CANCEL_REASON_CHECK = ("ck_orders_order_cancel_reason", "orders", "cancel_reason")
EVENT_TYPE_CHECK = ("ck_order_events_order_event_type", "order_events", "event_type")


def _replace_check(check: tuple[str, str, str], values: tuple[str, ...]) -> None:
    name, table, column = check
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(
        name,
        table,
        "{} IN ({})".format(column, ", ".join(f"'{value}'" for value in values)),
    )


def upgrade() -> None:
    _replace_check(PAYMENT_STATUS_CHECK, PAYMENT_STATUSES)
    _replace_check(RECEIPT_STATUS_CHECK, RECEIPT_STATUSES)
    _replace_check(CANCEL_REASON_CHECK, CANCEL_REASONS)
    _replace_check(EVENT_TYPE_CHECK, ORDER_EVENT_TYPES)

    op.drop_column("orders", "refunded_at")
    op.drop_column("payments", "refunded_at")
    op.drop_column("receipts", "annulled_at")
    op.drop_column("receipts", "annul_reason")
    op.execute("DELETE FROM message_templates WHERE key = 'order_refunded'")

    op.add_column(
        "payments",
        sa.Column("checkout_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "payments",
        sa.Column("receipt_sent", sa.Boolean(), server_default="false", nullable=False),
    )
    op.create_index(
        "ix_payments_reconcile",
        "payments",
        ["checkout_started_at"],
        postgresql_where=sa.text("paid_at IS NULL AND checkout_started_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_payments_reconcile", table_name="payments")
    op.drop_column("payments", "receipt_sent")
    op.drop_column("payments", "checkout_started_at")

    # Шаблон «Оплата возвращена» не восстанавливаем: без него бот возьмёт текст по умолчанию.
    op.add_column("receipts", sa.Column("annul_reason", sa.Text(), nullable=True))
    op.add_column(
        "receipts",
        sa.Column("annulled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "payments",
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "orders",
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.execute("UPDATE receipts SET status = 'pending' WHERE status = 'sent'")
    _replace_check(EVENT_TYPE_CHECK, OLD_ORDER_EVENT_TYPES)
    _replace_check(CANCEL_REASON_CHECK, OLD_CANCEL_REASONS)
    _replace_check(RECEIPT_STATUS_CHECK, OLD_RECEIPT_STATUSES)
    _replace_check(PAYMENT_STATUS_CHECK, OLD_PAYMENT_STATUSES)
