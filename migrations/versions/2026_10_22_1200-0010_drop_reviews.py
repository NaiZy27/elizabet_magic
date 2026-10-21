"""Отзывы убраны из проекта: таблица reviews удаляется вместе с данными.

Revision ID: 0010_drop_reviews
Revises: 0009_manual_delivery_prices
Create Date: 2026-10-22 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_drop_reviews"
down_revision: str | None = "0009_manual_delivery_prices"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("reviews")


def downgrade() -> None:
    # Пустая таблица в прежнем виде (как в 0005_reviews); удалённые отзывы не вернуть.
    op.create_table(
        "reviews",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False, start=1), nullable=False),
        sa.Column(
            "customer_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "customers.id",
                name="fk_reviews_customer_id_customers",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column(
            "order_id",
            sa.BigInteger(),
            sa.ForeignKey("orders.id", name="fk_reviews_order_id_orders", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("text", sa.Text(), server_default="", nullable=False),
        sa.Column("photo_file_id", sa.String(256), nullable=True),
        sa.Column("hidden_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_reviews"),
    )
    op.create_index("ix_reviews_customer_id", "reviews", ["customer_id"])
    op.create_index("ix_reviews_created_at", "reviews", ["created_at"])
