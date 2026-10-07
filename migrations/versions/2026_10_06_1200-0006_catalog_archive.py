"""Удаление боксов, вариантов и доп. услуг из панели.

Если позиция ни разу не попадала в заказ, она удаляется по-настоящему. Если попадала —
строку удалить нельзя (на неё ссылаются старые заказы), поэтому она уходит в архив:
archived_at заполнен, в панели и в боте её больше нет, история заказов не страдает.

Revision ID: 0006_catalog_archive
Revises: 0005_reviews
Create Date: 2026-10-06 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_catalog_archive"
down_revision: str | None = "0005_reviews"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("products", "product_variants", "addons")


def upgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    for table in TABLES:
        op.drop_column(table, "archived_at")
