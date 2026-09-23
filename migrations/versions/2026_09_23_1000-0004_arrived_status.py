"""Этап заказа «Доставлен в ПВЗ».

Раньше прибытие в пункт выдачи было отметкой (orders.arrived_at) у заказа в статусе
«Передан в доставку». Теперь это отдельный этап: своя колонка на доске и своё
уведомление клиенту. «Получен» остаётся последним — его ставит владелица или,
когда подключим службу доставки, сама служба.

Revision ID: 0004_arrived_status
Revises: 0003_pickup_search_text
Create Date: 2026-09-23 10:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_arrived_status"
down_revision: str | None = "0003_pickup_search_text"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


OLD_ORDER_STATUSES = (
    "draft",
    "waiting_payment",
    "queued",
    "assembling",
    "ready",
    "shipped",
    "completed",
    "cancelled",
)
ORDER_STATUSES = (*OLD_ORDER_STATUSES, "arrived")


def _check(values: tuple[str, ...]) -> str:
    return "status IN ({})".format(", ".join(f"'{value}'" for value in values))


def upgrade() -> None:
    op.drop_constraint("ck_orders_order_status", "orders", type_="check")
    op.create_check_constraint("ck_orders_order_status", "orders", _check(ORDER_STATUSES))

    # Заказы, у которых прибытие уже отмечено, переезжают на новый этап.
    op.execute(
        "UPDATE orders SET status = 'arrived', status_changed_at = now() "
        "WHERE status = 'shipped' AND arrived_at IS NOT NULL",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE orders SET status = 'shipped', status_changed_at = now() WHERE status = 'arrived'",
    )
    op.drop_constraint("ck_orders_order_status", "orders", type_="check")
    op.create_check_constraint("ck_orders_order_status", "orders", _check(OLD_ORDER_STATUSES))
