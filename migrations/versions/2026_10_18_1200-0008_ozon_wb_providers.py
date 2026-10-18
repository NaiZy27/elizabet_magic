"""Службы доставки Ozon и Wildberries.

Коды `ozon` и `wb` разрешены везде, где хранится служба: справочник служб, пункты
выдачи, заказ и отправление. Сама служба Wildberries заводится выключенной:
включается в панели, когда загружены её пункты. Отправку по ней владелица
оформляет вручную по данным из сообщения в боте. Ozon появится вместе с ключами
«Ozon Доставки для бизнеса» — запись службы для него пока не создаём.

Revision ID: 0008_ozon_wb_providers
Revises: 0007_robokassa_reconcile
Create Date: 2026-10-18 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008_ozon_wb_providers"
down_revision: str | None = "0007_robokassa_reconcile"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_PROVIDERS = ("cdek", "yandex")
PROVIDERS = (*OLD_PROVIDERS, "ozon", "wb")

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
    _replace_checks(PROVIDERS)
    # Резервная цена — как у уже заведённых служб, чтобы в боте осталась одна
    # «фиксированная» цена доставки.
    op.execute(
        """
        INSERT INTO delivery_providers (code, name, is_enabled, fallback_price_kopecks)
        SELECT 'wb', 'Wildberries', false,
               COALESCE((SELECT min(fallback_price_kopecks) FROM delivery_providers), 30000)
        WHERE NOT EXISTS (SELECT 1 FROM delivery_providers WHERE code = 'wb')
        """,
    )


def downgrade() -> None:
    op.execute("DELETE FROM delivery_providers WHERE code IN ('ozon', 'wb')")
    _replace_checks(OLD_PROVIDERS)
