"""Пункты выдачи без «контура»: переменная DELIVERY_ENVIRONMENT больше не нужна.

Контур (test/production) делил пункты на тестовые и боевые, пока службы были
подключены по API с тестовыми средами. Теперь все пункты настоящие — из публичных
списков, — и метка только мешала: смена переменной прятала весь справочник.

- если одна точка лежит в обоих контурах — остаётся одна (рабочая, обновлённая
  последней), ссылки заказов и покупателей переводятся на неё;
- демо-пункты из `make seed-demo` выключаются, если у СДЭК уже есть настоящие пункты:
  раньше их прятал тестовый контур, теперь клиенты увидели бы выдуманные адреса;
- колонка environment удаляется, уникальность — по (служба, код точки).

Revision ID: 0011_pickup_points_one_contour
Revises: 0010_drop_reviews
Create Date: 2026-10-22 13:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_pickup_points_one_contour"
down_revision: str | None = "0010_drop_reviews"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- дубли одной точки в двух контурах ---
    op.execute(
        """
        CREATE TEMP TABLE pickup_point_dupes ON COMMIT DROP AS
        SELECT id, keep_id FROM (
            SELECT id,
                   first_value(id) OVER w AS keep_id,
                   row_number() OVER w AS position
            FROM pickup_points
            WINDOW w AS (
                PARTITION BY provider, external_id
                ORDER BY is_active DESC, synced_at DESC NULLS LAST, id DESC
            )
        ) ranked
        WHERE position > 1
        """,
    )
    op.execute(
        """
        UPDATE orders SET pickup_point_id = d.keep_id
        FROM pickup_point_dupes d WHERE orders.pickup_point_id = d.id
        """,
    )
    op.execute(
        """
        UPDATE customers SET last_pickup_point_id = d.keep_id
        FROM pickup_point_dupes d WHERE customers.last_pickup_point_id = d.id
        """,
    )
    op.execute("DELETE FROM pickup_points WHERE id IN (SELECT id FROM pickup_point_dupes)")

    # --- демо-пункты не показываем рядом с настоящими ---
    op.execute(
        """
        UPDATE pickup_points SET is_active = false
        WHERE external_id LIKE 'demo-%'
          AND EXISTS (
              SELECT 1 FROM pickup_points real
              WHERE real.provider = 'cdek' AND real.external_id NOT LIKE 'demo-%'
                AND real.is_active
          )
        """,
    )

    # --- без колонки ---
    op.drop_index("ix_pickup_points_geo", table_name="pickup_points")
    op.drop_constraint(
        "uq_pickup_points_provider_environment_external_id", "pickup_points", type_="unique"
    )
    op.drop_constraint("ck_pickup_points_pickup_point_environment", "pickup_points", type_="check")
    op.drop_column("pickup_points", "environment")
    op.create_unique_constraint(
        "uq_pickup_points_provider_external_id", "pickup_points", ["provider", "external_id"]
    )
    op.create_index(
        "ix_pickup_points_geo", "pickup_points", ["provider", "is_active", "latitude"]
    )


def downgrade() -> None:
    # Все пункты возвращаются в тестовый контур — значение по умолчанию прежней переменной.
    op.drop_index("ix_pickup_points_geo", table_name="pickup_points")
    op.drop_constraint("uq_pickup_points_provider_external_id", "pickup_points", type_="unique")
    op.add_column(
        "pickup_points",
        sa.Column("environment", sa.String(32), server_default="test", nullable=False),
    )
    op.alter_column("pickup_points", "environment", server_default=None)
    op.create_check_constraint(
        "ck_pickup_points_pickup_point_environment",
        "pickup_points",
        "environment IN ('test', 'production')",
    )
    op.create_unique_constraint(
        "uq_pickup_points_provider_environment_external_id",
        "pickup_points",
        ["provider", "environment", "external_id"],
    )
    op.create_index(
        "ix_pickup_points_geo",
        "pickup_points",
        ["provider", "environment", "is_active", "latitude"],
    )
