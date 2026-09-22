"""Несколько боксов в заказе, чеки НПД, роли в панели, возвраты.

- пожелания (цвета, комментарий) переезжают с заказа на каждый бокс;
- услуга «за каждый бокс» привязывается к своему боксу (order_addons.order_item_id);
- чеки самозанятого — таблица receipts;
- причина отмены, отметка «прибыл в ПВЗ», возврат и сообщения бота с кнопками — у заказа;
- роль пользователя панели: владелица или сборщик.

Revision ID: 0002_boxes_receipts_roles
Revises: 0001_initial
Create Date: 2026-09-22 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_boxes_receipts_roles"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


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
)
ORDER_EVENT_TYPES = (
    *OLD_ORDER_EVENT_TYPES,
    "revived",
    "payment_after_cancel",
    "receipt_registered",
    "receipt_annulled",
    "refunded",
    "arrived_at_pickup",
)
CANCEL_REASONS = ("expired", "customer", "admin", "refund")
ADMIN_ROLES = ("owner", "assembler")
RECEIPT_STATUSES = ("pending", "registered", "annulled", "failed")
RECEIPT_PROVIDERS = ("robokassa", "manual", "stub")

# Тексты по умолчанию, которые поменялись: обновляем, только если владелица их не правила.
OLD_ORDER_CREATED = (
    "Спасибо! Заказ {{ order_number }} оформлен 💗\n\n"
    "{{ items }}\n\n"
    "Доставка: {{ delivery }}\n"
    "Итого: {{ total }}\n\n"
    "Чтобы мы начали сборку, оплатите заказ — ссылка действует до {{ pay_deadline }}."
)
NEW_ORDER_CREATED = OLD_ORDER_CREATED + "\nНомер заказа пришлём сразу после оплаты."
OLD_ORDER_PAID = (
    "Оплата получена, заказ {{ order_number }} принят в работу 💗\n\n"
    "Планируем собрать к {{ ready_date }} и сразу передадим в доставку.\n"
    "Чек придёт на указанный вами телефон или email."
)
NEW_ORDER_PAID = (
    "Оплата получена! Ваш заказ №{{ order_number }} принят 💗\n\n"
    "Планируем собрать к {{ ready_date }} и сразу передадим в доставку.\n"
    "Чек придёт на {{ email }}."
)


def enum_check(column: str, values: tuple[str, ...], name: str) -> sa.CheckConstraint:
    listed = ", ".join(f"'{value}'" for value in values)
    return sa.CheckConstraint(f"{column} IN ({listed})", name=name)


def jsonb_list(name: str) -> sa.Column:
    return sa.Column(
        name,
        postgresql.JSONB(astext_type=sa.Text()),
        server_default="[]",
        nullable=False,
    )


def upgrade() -> None:
    # --- пожелания на каждый бокс ---
    op.add_column("order_items", jsonb_list("favorite_colors"))
    op.add_column("order_items", jsonb_list("avoid_colors"))
    op.add_column("order_items", sa.Column("comment", sa.Text(), nullable=True))

    # Старые заказы — однобоксовые: пожелания заказа переносим на его первый бокс.
    # Цвета хранились id палитры, теперь — названиями; свой текст дописываем в конец.
    op.execute(
        """
        UPDATE order_items AS oi
        SET favorite_colors = COALESCE(
                (SELECT jsonb_agg(c.name ORDER BY c.sort_order, c.id)
                   FROM colors c
                  WHERE c.id::text IN (
                      SELECT jsonb_array_elements_text(
                          COALESCE(o.favorite_colors -> 'colors', '[]'::jsonb)))),
                '[]'::jsonb)
            || CASE WHEN COALESCE(o.favorite_colors ->> 'custom', '') <> ''
                    THEN jsonb_build_array(o.favorite_colors ->> 'custom')
                    ELSE '[]'::jsonb END,
            avoid_colors = COALESCE(
                (SELECT jsonb_agg(c.name ORDER BY c.sort_order, c.id)
                   FROM colors c
                  WHERE c.id::text IN (
                      SELECT jsonb_array_elements_text(
                          COALESCE(o.avoid_colors -> 'colors', '[]'::jsonb)))),
                '[]'::jsonb)
            || CASE WHEN COALESCE(o.avoid_colors ->> 'custom', '') <> ''
                    THEN jsonb_build_array(o.avoid_colors ->> 'custom')
                    ELSE '[]'::jsonb END,
            comment = o.customer_comment
        FROM orders AS o
        WHERE o.id = oi.order_id
          AND oi.id = (SELECT min(first.id) FROM order_items first WHERE first.order_id = o.id)
        """,
    )
    op.drop_column("orders", "favorite_colors")
    op.drop_column("orders", "avoid_colors")
    op.drop_column("orders", "customer_comment")

    # --- услуги на конкретный бокс ---
    op.add_column("order_addons", sa.Column("order_item_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_order_addons_order_item_id_order_items",
        "order_addons",
        "order_items",
        ["order_item_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_order_addons_order_item_id", "order_addons", ["order_item_id"])
    op.execute(
        """
        UPDATE order_addons AS oa
        SET order_item_id = (
            SELECT min(oi.id) FROM order_items oi WHERE oi.order_id = oa.order_id
        )
        WHERE oa.charge_mode_snapshot = 'per_box'
        """,
    )
    op.drop_constraint("uq_order_addons_order_addon", "order_addons", type_="unique")
    op.create_index(
        "uq_order_addons_item_addon",
        "order_addons",
        ["order_item_id", "addon_id"],
        unique=True,
        postgresql_where=sa.text("order_item_id IS NOT NULL"),
    )
    op.create_index(
        "uq_order_addons_order_addon",
        "order_addons",
        ["order_id", "addon_id"],
        unique=True,
        postgresql_where=sa.text("order_item_id IS NULL"),
    )

    # --- заказ: отмена, прибытие, возврат, кнопки в боте ---
    op.add_column("orders", sa.Column("arrived_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("orders", sa.Column("cancel_reason", sa.String(32), nullable=True))
    op.add_column("orders", sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("orders", jsonb_list("bot_messages"))
    op.create_check_constraint(
        "ck_orders_order_cancel_reason",
        "orders",
        "cancel_reason IN ({})".format(", ".join(f"'{value}'" for value in CANCEL_REASONS)),
    )
    op.execute(
        """
        UPDATE orders AS o SET cancel_reason = 'expired'
        WHERE o.status = 'cancelled'
          AND EXISTS (
              SELECT 1 FROM order_events e WHERE e.order_id = o.id AND e.event_type = 'expired'
          )
        """,
    )

    op.drop_constraint("ck_order_events_order_event_type", "order_events", type_="check")
    op.create_check_constraint(
        "ck_order_events_order_event_type",
        "order_events",
        "event_type IN ({})".format(", ".join(f"'{value}'" for value in ORDER_EVENT_TYPES)),
    )

    # --- возврат у платежа и чеки ---
    op.add_column("payments", sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "receipts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False, start=1), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("payment_id", sa.BigInteger(), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("amount_kopecks", sa.BigInteger(), nullable=False),
        sa.Column("buyer_email", sa.String(256), nullable=True),
        jsonb_list("items"),
        sa.Column("external_id", sa.String(128), nullable=True),
        sa.Column("url", sa.String(512), nullable=True),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("annulled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("annul_reason", sa.Text(), nullable=True),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_receipts"),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name="fk_receipts_order_id_orders",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["payment_id"],
            ["payments.id"],
            name="fk_receipts_payment_id_payments",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("payment_id", name="uq_receipts_payment"),
        sa.CheckConstraint(
            "amount_kopecks >= 0",
            name="ck_receipts_amount_kopecks_non_negative",
        ),
        enum_check("provider", RECEIPT_PROVIDERS, "ck_receipts_receipt_provider"),
        enum_check("status", RECEIPT_STATUSES, "ck_receipts_receipt_status"),
    )
    op.create_index("ix_receipts_order", "receipts", ["order_id"])

    # --- роли в панели: все, кто уже есть, — владельцы ---
    op.add_column(
        "admin_users",
        sa.Column("role", sa.String(32), server_default="owner", nullable=False),
    )
    op.create_check_constraint(
        "ck_admin_users_admin_role",
        "admin_users",
        "role IN ({})".format(", ".join(f"'{value}'" for value in ADMIN_ROLES)),
    )

    # --- тексты по умолчанию ---
    templates = sa.table(
        "message_templates",
        sa.column("key", sa.String),
        sa.column("text", sa.Text),
    )
    for key, old, new in (
        ("order_created", OLD_ORDER_CREATED, NEW_ORDER_CREATED),
        ("order_paid", OLD_ORDER_PAID, NEW_ORDER_PAID),
    ):
        op.execute(
            templates.update()
            .where(templates.c.key == key, templates.c.text == old)
            .values(text=new),
        )


def downgrade() -> None:
    templates = sa.table(
        "message_templates",
        sa.column("key", sa.String),
        sa.column("text", sa.Text),
    )
    for key, old, new in (
        ("order_created", OLD_ORDER_CREATED, NEW_ORDER_CREATED),
        ("order_paid", OLD_ORDER_PAID, NEW_ORDER_PAID),
    ):
        op.execute(
            templates.update()
            .where(templates.c.key == key, templates.c.text == new)
            .values(text=old),
        )

    op.drop_constraint("ck_admin_users_admin_role", "admin_users", type_="check")
    op.drop_column("admin_users", "role")

    op.drop_index("ix_receipts_order", table_name="receipts")
    op.drop_table("receipts")
    op.drop_column("payments", "refunded_at")

    op.execute(
        "DELETE FROM order_events WHERE event_type NOT IN ({})".format(
            ", ".join(f"'{value}'" for value in OLD_ORDER_EVENT_TYPES),
        ),
    )
    op.drop_constraint("ck_order_events_order_event_type", "order_events", type_="check")
    op.create_check_constraint(
        "ck_order_events_order_event_type",
        "order_events",
        "event_type IN ({})".format(", ".join(f"'{value}'" for value in OLD_ORDER_EVENT_TYPES)),
    )

    op.drop_constraint("ck_orders_order_cancel_reason", "orders", type_="check")
    op.drop_column("orders", "bot_messages")
    op.drop_column("orders", "refunded_at")
    op.drop_column("orders", "cancel_reason")
    op.drop_column("orders", "arrived_at")

    # Откат к «одна услуга на заказ»: при нескольких боксах с видео строки сольются в одну.
    op.drop_index("uq_order_addons_order_addon", table_name="order_addons")
    op.drop_index("uq_order_addons_item_addon", table_name="order_addons")
    op.execute(
        """
        DELETE FROM order_addons AS oa
        USING order_addons AS keep
        WHERE oa.order_id = keep.order_id AND oa.addon_id = keep.addon_id AND oa.id > keep.id
        """,
    )
    op.create_unique_constraint(
        "uq_order_addons_order_addon",
        "order_addons",
        ["order_id", "addon_id"],
    )
    op.drop_index("ix_order_addons_order_item_id", table_name="order_addons")
    op.drop_constraint(
        "fk_order_addons_order_item_id_order_items",
        "order_addons",
        type_="foreignkey",
    )
    op.drop_column("order_addons", "order_item_id")

    # Пожелания возвращаются на заказ — с первого бокса, названиями в поле «свой текст».
    op.add_column(
        "orders",
        sa.Column(
            "favorite_colors",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
    )
    op.add_column(
        "orders",
        sa.Column(
            "avoid_colors",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
    )
    op.add_column("orders", sa.Column("customer_comment", sa.Text(), nullable=True))
    op.execute(
        """
        UPDATE orders AS o
        SET favorite_colors = jsonb_build_object(
                'colors', '[]'::jsonb,
                'custom', (SELECT string_agg(value, ', ')
                             FROM jsonb_array_elements_text(oi.favorite_colors))),
            avoid_colors = jsonb_build_object(
                'colors', '[]'::jsonb,
                'custom', (SELECT string_agg(value, ', ')
                             FROM jsonb_array_elements_text(oi.avoid_colors))),
            customer_comment = oi.comment
        FROM order_items AS oi
        WHERE oi.order_id = o.id
          AND oi.id = (SELECT min(first.id) FROM order_items first WHERE first.order_id = o.id)
        """,
    )
    op.drop_column("order_items", "comment")
    op.drop_column("order_items", "avoid_colors")
    op.drop_column("order_items", "favorite_colors")
