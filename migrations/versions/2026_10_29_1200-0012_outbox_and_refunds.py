"""Очередь исходящих сообщений в базе и список «к возврату».

- outbox: уведомления клиенту и владелице записываются в той же транзакции, что
  и изменение заказа, а отправляет их обработчик воркера. Сообщение не теряется,
  если в момент постановки недоступен Redis или воркер перезапускается посреди
  отправки: оно остаётся в базе и уходит при следующем проходе;
- refunds: оплаты, деньги по которым нужно вернуть (оплата после отмены, повторная
  оплата, отмена уже оплаченного заказа). Сам возврат по-прежнему делается в кабинете
  Робокассы — здесь только список и отметка «вернула»;
- события заказа «refund_required» и «refund_done».

Revision ID: 0012_outbox_and_refunds
Revises: 0011_pickup_points_one_contour
Create Date: 2026-10-29 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_outbox_and_refunds"
down_revision: str | None = "0011_pickup_points_one_contour"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OUTBOX_KINDS = ("customer", "owner_order", "owner_text", "alert", "strip_buttons")
OUTBOX_STATUSES = ("pending", "sent", "failed")
REFUND_REASONS = ("paid_after_cancel", "duplicate_payment", "cancelled_after_payment")
REFUND_STATUSES = ("pending", "done")

OLD_ORDER_EVENT_TYPES = (
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
ORDER_EVENT_TYPES = (*OLD_ORDER_EVENT_TYPES, "refund_required", "refund_done")
EVENT_TYPE_CHECK = "ck_order_events_order_event_type"


def enum_check(column: str, values: tuple[str, ...], name: str) -> sa.CheckConstraint:
    """CHECK по списку допустимых кодов."""
    listed = ", ".join(f"'{value}'" for value in values)
    return sa.CheckConstraint(f"{column} IN ({listed})", name=name)


def timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def _replace_event_check(values: tuple[str, ...]) -> None:
    op.drop_constraint(EVENT_TYPE_CHECK, "order_events", type_="check")
    op.create_check_constraint(
        EVENT_TYPE_CHECK,
        "order_events",
        "event_type IN ({})".format(", ".join(f"'{value}'" for value in values)),
    )


def upgrade() -> None:
    op.create_table(
        "outbox",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False, start=1), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        *timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_outbox"),
        enum_check("kind", OUTBOX_KINDS, "ck_outbox_outbox_kind"),
        enum_check("status", OUTBOX_STATUSES, "ck_outbox_outbox_status"),
    )
    op.create_index(
        "ix_outbox_due",
        "outbox",
        ["next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "refunds",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False, start=1), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("payment_id", sa.BigInteger(), nullable=False),
        sa.Column("amount_kopecks", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_by", sa.String(64), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        *timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_refunds"),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name="fk_refunds_order_id_orders",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["payment_id"],
            ["payments.id"],
            name="fk_refunds_payment_id_payments",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("payment_id", name="uq_refunds_payment"),
        sa.CheckConstraint("amount_kopecks >= 0", name="ck_refunds_amount_kopecks_non_negative"),
        enum_check("reason", REFUND_REASONS, "ck_refunds_refund_reason"),
        enum_check("status", REFUND_STATUSES, "ck_refunds_refund_status"),
    )
    op.create_index("ix_refunds_order", "refunds", ["order_id"])
    op.create_index(
        "ix_refunds_pending",
        "refunds",
        ["created_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )

    _replace_event_check(ORDER_EVENT_TYPES)


def downgrade() -> None:
    op.execute("DELETE FROM order_events WHERE event_type IN ('refund_required', 'refund_done')")
    _replace_event_check(OLD_ORDER_EVENT_TYPES)
    op.drop_index("ix_refunds_pending", table_name="refunds")
    op.drop_index("ix_refunds_order", table_name="refunds")
    op.drop_table("refunds")
    op.drop_index("ix_outbox_due", table_name="outbox")
    op.drop_table("outbox")
