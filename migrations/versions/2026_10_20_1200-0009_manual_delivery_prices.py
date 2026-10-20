"""Все службы ручные: две цены доставки у каждой, без Wildberries.

- delivery_providers: вместо одной резервной цены — две, задаются в панели:
  `moscow_price_kopecks` (пункт в Москве) и `region_price_kopecks` (остальная
  Россия). Текущая цена переносится в обе;
- Wildberries убрана: у заказчицы её нет. Пункты и запись службы удаляются,
  код `wb` больше не разрешён;
- Ozon заводится выключенной ручной службой — включается, когда будут её пункты.

Revision ID: 0009_manual_delivery_prices
Revises: 0008_ozon_wb_providers
Create Date: 2026-10-20 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_manual_delivery_prices"
down_revision: str | None = "0008_ozon_wb_providers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_PROVIDERS = ("cdek", "yandex", "ozon", "wb")
PROVIDERS = ("cdek", "yandex", "ozon")

#: (имя ограничения, таблица, колонка)
CHECKS = (
    ("ck_delivery_providers_delivery_provider_code", "delivery_providers", "code"),
    ("ck_pickup_points_pickup_point_provider", "pickup_points", "provider"),
    ("ck_orders_order_delivery_provider", "orders", "delivery_provider"),
    ("ck_shipments_shipment_provider", "shipments", "provider"),
)


def _replace_checks(values: tuple[str, ...]) -> None:
    for name, table, column in CHECKS:
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(
            name,
            table,
            "{} IN ({})".format(column, ", ".join(f"'{value}'" for value in values)),
        )


def upgrade() -> None:
    # --- две цены ---
    op.drop_constraint(
        "ck_delivery_providers_fallback_price_kopecks_non_negative",
        "delivery_providers",
        type_="check",
    )
    op.alter_column(
        "delivery_providers",
        "fallback_price_kopecks",
        new_column_name="region_price_kopecks",
        server_default="50000",
    )
    op.add_column(
        "delivery_providers",
        sa.Column("moscow_price_kopecks", sa.BigInteger(), server_default="50000", nullable=False),
    )
    op.execute("UPDATE delivery_providers SET moscow_price_kopecks = region_price_kopecks")
    op.create_check_constraint(
        "region_price_kopecks_non_negative",
        "delivery_providers",
        "region_price_kopecks >= 0",
    )
    op.create_check_constraint(
        "moscow_price_kopecks_non_negative",
        "delivery_providers",
        "moscow_price_kopecks >= 0",
    )

    # --- без Wildberries ---
    op.execute("DELETE FROM pickup_points WHERE provider = 'wb'")
    op.execute("DELETE FROM delivery_providers WHERE code = 'wb'")
    _replace_checks(PROVIDERS)

    # --- Ozon: ручная служба, пока выключена ---
    op.execute(
        """
        INSERT INTO delivery_providers (code, name, is_enabled, moscow_price_kopecks,
                                        region_price_kopecks)
        SELECT 'ozon', 'Ozon', false,
               COALESCE((SELECT min(moscow_price_kopecks) FROM delivery_providers), 50000),
               COALESCE((SELECT min(region_price_kopecks) FROM delivery_providers), 50000)
        WHERE NOT EXISTS (SELECT 1 FROM delivery_providers WHERE code = 'ozon')
        """,
    )


def downgrade() -> None:
    op.execute("DELETE FROM delivery_providers WHERE code = 'ozon'")
    _replace_checks(OLD_PROVIDERS)

    op.drop_constraint("moscow_price_kopecks_non_negative", "delivery_providers", type_="check")
    op.drop_constraint("region_price_kopecks_non_negative", "delivery_providers", type_="check")
    op.drop_column("delivery_providers", "moscow_price_kopecks")
    op.alter_column(
        "delivery_providers",
        "region_price_kopecks",
        new_column_name="fallback_price_kopecks",
        server_default="30000",
    )
    op.create_check_constraint(
        "ck_delivery_providers_fallback_price_kopecks_non_negative",
        "delivery_providers",
        "fallback_price_kopecks >= 0",
    )
