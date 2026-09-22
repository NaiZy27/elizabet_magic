"""Строка поиска у пунктов выдачи.

Поиск шёл через lower() в базе, а у базы с локалью C lower() не меняет регистр
кириллицы — «москва» не находила «Москва». Теперь строка поиска готовится в Python
(core.text.normalize_search) и хранится в pickup_points.search_text.

Revision ID: 0003_pickup_search_text
Revises: 0002_boxes_receipts_roles
Create Date: 2026-09-22 18:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_pickup_search_text"
down_revision: str | None = "0002_boxes_receipts_roles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _normalize(value: str) -> str:
    """Копия core.text.normalize_search на момент миграции: код может поменяться, миграция — нет."""
    folded = value.casefold().replace("ё", "е")
    cleaned = "".join(character if character.isalnum() else " " for character in folded)
    return " ".join(cleaned.split())


def upgrade() -> None:
    op.add_column(
        "pickup_points",
        sa.Column("search_text", sa.Text(), server_default="", nullable=False),
    )

    points = sa.table(
        "pickup_points",
        sa.column("id", sa.BigInteger),
        sa.column("city", sa.String),
        sa.column("address", sa.String),
        sa.column("name", sa.String),
        sa.column("search_text", sa.Text),
    )
    connection = op.get_bind()
    rows = connection.execute(sa.select(points.c.id, points.c.city, points.c.address, points.c.name))
    for point_id, city, address, name in rows.fetchall():
        connection.execute(
            points.update()
            .where(points.c.id == point_id)
            .values(search_text=_normalize(f"{city} {address} {name}")),
        )


def downgrade() -> None:
    op.drop_column("pickup_points", "search_text")
