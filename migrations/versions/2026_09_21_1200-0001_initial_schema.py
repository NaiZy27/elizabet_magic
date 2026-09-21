"""Начальная схема: каталог, клиенты, заказы, оплата, доставка, настройки.

Статусы и коды хранятся как VARCHAR с CHECK по списку значений — так их проще
расширять, чем нативный ENUM Postgres.

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-21 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


ORDER_STATUSES = (
    "draft",
    "waiting_payment",
    "queued",
    "assembling",
    "ready",
    "shipped",
    "completed",
    "cancelled",
)
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
)
PAYMENT_STATUSES = ("pending", "paid", "failed", "cancelled", "refunded")
PAYMENT_PROVIDERS = ("robokassa", "stub")
PROVIDER_ENVIRONMENTS = ("test", "production")
DELIVERY_PROVIDERS = ("cdek", "yandex")
SHIPMENT_STATUSES = ("created", "in_transit", "arrived", "delivered", "returned", "cancelled")
ADDON_CHARGE_MODES = ("per_box", "per_order")


def enum_check(column: str, values: tuple[str, ...], name: str) -> sa.CheckConstraint:
    """CHECK по списку допустимых кодов."""
    listed = ", ".join(f"'{value}'" for value in values)
    return sa.CheckConstraint(f"{column} IN ({listed})", name=name)


def pk() -> sa.Column:
    """Ключ таблицы. Имя ограничения задаётся отдельным PrimaryKeyConstraint."""
    return sa.Column("id", sa.BigInteger(), sa.Identity(always=False, start=1), nullable=False)


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


def upgrade() -> None:
    # --- каталог ---
    op.create_table(
        "products",
        pk(),
        sa.Column("sku", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("photo_file_id", sa.String(256), nullable=True),
        sa.Column("production_days", sa.Integer(), server_default="1", nullable=False),
        sa.Column("extra_spoon_price_kopecks", sa.BigInteger(), nullable=True),
        sa.Column("max_spoon_count", sa.Integer(), nullable=True),
        sa.Column("sort_order", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        *timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_products"),
        sa.UniqueConstraint("sku", name="uq_products_sku"),
        sa.CheckConstraint("production_days > 0", name="ck_products_production_days_positive"),
        sa.CheckConstraint(
            "extra_spoon_price_kopecks >= 0",
            name="ck_products_extra_spoon_price_kopecks_non_negative",
        ),
        sa.CheckConstraint("max_spoon_count > 0", name="ck_products_max_spoon_count_positive"),
        sa.CheckConstraint(
            "(extra_spoon_price_kopecks IS NULL) = (max_spoon_count IS NULL)",
            name="ck_products_extra_spoon_settings_complete",
        ),
    )

    op.create_table(
        "product_variants",
        pk(),
        sa.Column("product_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("sku", sa.String(64), nullable=False),
        sa.Column("spoon_count", sa.Integer(), nullable=False),
        sa.Column("price_kopecks", sa.BigInteger(), nullable=False),
        sa.Column("weight_g", sa.Integer(), nullable=True),
        sa.Column("length_cm", sa.Integer(), nullable=True),
        sa.Column("width_cm", sa.Integer(), nullable=True),
        sa.Column("height_cm", sa.Integer(), nullable=True),
        sa.Column("sort_order", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        *timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_product_variants"),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name="fk_product_variants_product_id_products",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("sku", name="uq_product_variants_sku"),
        sa.CheckConstraint("spoon_count > 0", name="ck_product_variants_spoon_count_positive"),
        sa.CheckConstraint(
            "price_kopecks >= 0",
            name="ck_product_variants_price_kopecks_non_negative",
        ),
        sa.CheckConstraint("weight_g > 0", name="ck_product_variants_weight_g_positive"),
        sa.CheckConstraint("length_cm > 0", name="ck_product_variants_length_cm_positive"),
        sa.CheckConstraint("width_cm > 0", name="ck_product_variants_width_cm_positive"),
        sa.CheckConstraint("height_cm > 0", name="ck_product_variants_height_cm_positive"),
    )
    op.create_index("ix_product_variants_product_id", "product_variants", ["product_id"])

    op.create_table(
        "addons",
        pk(),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("price_kopecks", sa.BigInteger(), nullable=False),
        sa.Column("charge_mode", sa.String(32), nullable=False),
        sa.Column("extra_production_days", sa.Integer(), server_default="0", nullable=False),
        sa.Column("sort_order", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        *timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_addons"),
        sa.UniqueConstraint("code", name="uq_addons_code"),
        sa.CheckConstraint("price_kopecks >= 0", name="ck_addons_price_kopecks_non_negative"),
        sa.CheckConstraint(
            "extra_production_days >= 0",
            name="ck_addons_extra_production_days_non_negative",
        ),
        enum_check("charge_mode", ADDON_CHARGE_MODES, "ck_addons_addon_charge_mode"),
    )

    op.create_table(
        "colors",
        pk(),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("sort_order", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        *timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_colors"),
        sa.UniqueConstraint("name", name="uq_colors_name"),
    )

    # --- доставка ---
    op.create_table(
        "delivery_providers",
        pk(),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "fallback_price_kopecks",
            sa.BigInteger(),
            server_default="30000",
            nullable=False,
        ),
        sa.Column(
            "settings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("pickup_points_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_error", sa.Text(), nullable=True),
        *timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_delivery_providers"),
        sa.UniqueConstraint("code", name="uq_delivery_providers_code"),
        sa.CheckConstraint(
            "fallback_price_kopecks >= 0",
            name="ck_delivery_providers_fallback_price_kopecks_non_negative",
        ),
        enum_check("code", DELIVERY_PROVIDERS, "ck_delivery_providers_delivery_provider_code"),
    )

    op.create_table(
        "pickup_points",
        pk(),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("environment", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(128), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("city", sa.String(128), nullable=False),
        sa.Column("city_code", sa.String(64), nullable=True),
        sa.Column("address", sa.String(512), nullable=False),
        sa.Column("latitude", sa.Double(), nullable=False),
        sa.Column("longitude", sa.Double(), nullable=False),
        sa.Column("working_hours", sa.String(256), nullable=True),
        sa.Column("max_weight_g", sa.Integer(), nullable=True),
        sa.Column("max_dimensions_cm", sa.String(64), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        *timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_pickup_points"),
        sa.UniqueConstraint(
            "provider",
            "environment",
            "external_id",
            name="uq_pickup_points_provider_environment_external_id",
        ),
        sa.CheckConstraint("latitude BETWEEN -90 AND 90", name="ck_pickup_points_latitude_range"),
        sa.CheckConstraint(
            "longitude BETWEEN -180 AND 180",
            name="ck_pickup_points_longitude_range",
        ),
        sa.CheckConstraint("max_weight_g > 0", name="ck_pickup_points_max_weight_g_positive"),
        enum_check("provider", DELIVERY_PROVIDERS, "ck_pickup_points_pickup_point_provider"),
        enum_check(
            "environment",
            PROVIDER_ENVIRONMENTS,
            "ck_pickup_points_pickup_point_environment",
        ),
    )
    op.create_index(
        "ix_pickup_points_geo",
        "pickup_points",
        ["provider", "environment", "is_active", "latitude"],
    )
    op.create_index("ix_pickup_points_city", "pickup_points", ["city"])

    # --- клиенты ---
    op.create_table(
        "customers",
        pk(),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(64), nullable=True),
        sa.Column("full_name", sa.String(256), nullable=True),
        sa.Column("phone", sa.String(32), nullable=True),
        sa.Column("email", sa.String(256), nullable=True),
        sa.Column("last_pickup_point_id", sa.BigInteger(), nullable=True),
        sa.Column("consent_accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bot_blocked_at", sa.DateTime(timezone=True), nullable=True),
        *timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_customers"),
        sa.UniqueConstraint("telegram_user_id", name="uq_customers_telegram_user_id"),
        sa.ForeignKeyConstraint(
            ["last_pickup_point_id"],
            ["pickup_points.id"],
            name="fk_customers_last_pickup_point_id_pickup_points",
            ondelete="RESTRICT",
        ),
    )

    # --- заказы ---
    op.execute(sa.schema.CreateSequence(sa.Sequence("order_number_seq", start=1001)))

    op.create_table(
        "orders",
        pk(),
        sa.Column("number", sa.String(16), nullable=True),
        sa.Column("customer_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "status_changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("recipient_name", sa.String(256), nullable=True),
        sa.Column("recipient_phone", sa.String(32), nullable=True),
        sa.Column("recipient_email", sa.String(256), nullable=True),
        sa.Column("subtotal_kopecks", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("delivery_kopecks", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("total_kopecks", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "favorite_colors",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "avoid_colors",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("customer_comment", sa.Text(), nullable=True),
        sa.Column("admin_note", sa.Text(), nullable=True),
        sa.Column("production_days", sa.Integer(), server_default="1", nullable=False),
        sa.Column("queue_position", sa.BigInteger(), nullable=True),
        sa.Column("promised_ready_date", sa.Date(), nullable=True),
        sa.Column("delivery_provider", sa.String(32), nullable=True),
        sa.Column("pickup_point_id", sa.BigInteger(), nullable=True),
        sa.Column("pickup_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        *timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_orders"),
        sa.UniqueConstraint("number", name="uq_orders_number"),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name="fk_orders_customer_id_customers",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["pickup_point_id"],
            ["pickup_points.id"],
            name="fk_orders_pickup_point_id_pickup_points",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "subtotal_kopecks >= 0",
            name="ck_orders_subtotal_kopecks_non_negative",
        ),
        sa.CheckConstraint(
            "delivery_kopecks >= 0",
            name="ck_orders_delivery_kopecks_non_negative",
        ),
        sa.CheckConstraint("total_kopecks >= 0", name="ck_orders_total_kopecks_non_negative"),
        sa.CheckConstraint(
            "total_kopecks = subtotal_kopecks + delivery_kopecks",
            name="ck_orders_total_matches_parts",
        ),
        sa.CheckConstraint("production_days > 0", name="ck_orders_production_days_positive"),
        sa.CheckConstraint(
            "queue_position IS NULL OR "
            "(queue_position > 0 AND status IN ('queued', 'assembling'))",
            name="ck_orders_queue_position_only_in_queue",
        ),
        enum_check("status", ORDER_STATUSES, "ck_orders_order_status"),
        enum_check(
            "delivery_provider",
            DELIVERY_PROVIDERS,
            "ck_orders_order_delivery_provider",
        ),
    )
    # Перестановка карточек на доске на мгновение создаёт одинаковые позиции,
    # поэтому уникальность проверяется в конце транзакции.
    op.execute(
        "ALTER TABLE orders ADD CONSTRAINT uq_orders_queue_position "
        "UNIQUE (queue_position) DEFERRABLE INITIALLY DEFERRED"
    )
    op.create_index(
        "uq_orders_single_draft",
        "orders",
        ["customer_id"],
        unique=True,
        postgresql_where=sa.text("status = 'draft'"),
    )
    op.create_index("ix_orders_customer_created", "orders", ["customer_id", "created_at"])
    op.create_index("ix_orders_status_created", "orders", ["status", "created_at"])
    op.create_index("ix_orders_pickup_point_id", "orders", ["pickup_point_id"])

    op.create_table(
        "order_items",
        pk(),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("variant_id", sa.BigInteger(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("spoon_count", sa.Integer(), nullable=False),
        sa.Column("unit_price_kopecks", sa.BigInteger(), nullable=False),
        sa.Column("name_snapshot", sa.String(256), nullable=False),
        *timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_order_items"),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name="fk_order_items_order_id_orders",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["variant_id"],
            ["product_variants.id"],
            name="fk_order_items_variant_id_product_variants",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("quantity > 0", name="ck_order_items_quantity_positive"),
        sa.CheckConstraint("spoon_count > 0", name="ck_order_items_spoon_count_positive"),
        sa.CheckConstraint(
            "unit_price_kopecks >= 0",
            name="ck_order_items_unit_price_kopecks_non_negative",
        ),
    )
    op.create_index("ix_order_items_order", "order_items", ["order_id"])

    op.create_table(
        "order_addons",
        pk(),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("addon_id", sa.BigInteger(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price_kopecks", sa.BigInteger(), nullable=False),
        sa.Column("name_snapshot", sa.String(128), nullable=False),
        sa.Column("code_snapshot", sa.String(32), nullable=False),
        sa.Column("charge_mode_snapshot", sa.String(32), nullable=False),
        *timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_order_addons"),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name="fk_order_addons_order_id_orders",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["addon_id"],
            ["addons.id"],
            name="fk_order_addons_addon_id_addons",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("order_id", "addon_id", name="uq_order_addons_order_addon"),
        sa.CheckConstraint("quantity > 0", name="ck_order_addons_quantity_positive"),
        sa.CheckConstraint(
            "unit_price_kopecks >= 0",
            name="ck_order_addons_unit_price_kopecks_non_negative",
        ),
        enum_check(
            "charge_mode_snapshot",
            ADDON_CHARGE_MODES,
            "ck_order_addons_order_addon_charge_mode",
        ),
    )

    op.create_table(
        "order_events",
        pk(),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("actor", sa.String(32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_order_events"),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name="fk_order_events_order_id_orders",
            ondelete="CASCADE",
        ),
        enum_check("event_type", ORDER_EVENT_TYPES, "ck_order_events_order_event_type"),
    )
    op.create_index("ix_order_events_order_created", "order_events", ["order_id", "created_at"])

    # --- оплата ---
    op.create_table(
        "payments",
        pk(),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("environment", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(128), nullable=True),
        sa.Column("public_token", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount_kopecks", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("raw_result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("fee_kopecks", sa.BigInteger(), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        *timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_payments"),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name="fk_payments_order_id_orders",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("public_token", name="uq_payments_public_token"),
        sa.UniqueConstraint(
            "provider",
            "environment",
            "external_id",
            name="uq_payments_provider_environment_external_id",
        ),
        sa.CheckConstraint("amount_kopecks >= 0", name="ck_payments_amount_kopecks_non_negative"),
        sa.CheckConstraint("fee_kopecks >= 0", name="ck_payments_fee_kopecks_non_negative"),
        enum_check("provider", PAYMENT_PROVIDERS, "ck_payments_payment_provider"),
        enum_check("environment", PROVIDER_ENVIRONMENTS, "ck_payments_payment_environment"),
        enum_check("status", PAYMENT_STATUSES, "ck_payments_payment_status"),
    )
    op.create_index("ix_payments_order_id", "payments", ["order_id"])
    op.create_index(
        "uq_payments_pending_order",
        "payments",
        ["order_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "uq_payments_paid_order",
        "payments",
        ["order_id"],
        unique=True,
        postgresql_where=sa.text("status = 'paid'"),
    )

    op.create_table(
        "shipments",
        pk(),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(128), nullable=False),
        sa.Column("track_number", sa.String(64), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("status_raw", sa.String(128), nullable=True),
        sa.Column("status_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("label_url", sa.String(512), nullable=True),
        *timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_shipments"),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name="fk_shipments_order_id_orders",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "provider",
            "external_id",
            name="uq_shipments_provider_external_id",
        ),
        enum_check("provider", DELIVERY_PROVIDERS, "ck_shipments_shipment_provider"),
        enum_check("status", SHIPMENT_STATUSES, "ck_shipments_shipment_status"),
    )
    op.create_index("ix_shipments_order_id", "shipments", ["order_id"])
    op.create_index("ix_shipments_track_number", "shipments", ["track_number"])
    op.create_index(
        "uq_shipments_active_order",
        "shipments",
        ["order_id"],
        unique=True,
        postgresql_where=sa.text("status <> 'cancelled'"),
    )

    # --- настройки ---
    op.create_table(
        "message_templates",
        pk(),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default="true", nullable=False),
        *timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_message_templates"),
        sa.UniqueConstraint("key", name="uq_message_templates_key"),
    )

    op.create_table(
        "settings",
        pk(),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column(
            "value",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        *timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_settings"),
        sa.UniqueConstraint("key", name="uq_settings_key"),
    )

    op.create_table(
        "admin_users",
        pk(),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.String(128), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        *timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_admin_users"),
        sa.UniqueConstraint("username", name="uq_admin_users_username"),
    )


def downgrade() -> None:
    op.drop_table("admin_users")
    op.drop_table("settings")
    op.drop_table("message_templates")
    op.drop_table("shipments")
    op.drop_table("payments")
    op.drop_table("order_events")
    op.drop_table("order_addons")
    op.drop_table("order_items")
    op.drop_table("orders")
    op.execute(sa.schema.DropSequence(sa.Sequence("order_number_seq")))
    op.drop_table("customers")
    op.drop_table("pickup_points")
    op.drop_table("delivery_providers")
    op.drop_table("colors")
    op.drop_table("addons")
    op.drop_table("product_variants")
    op.drop_table("products")
