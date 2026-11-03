"""Вместо боксов — ложечки; список цветов убран.

- каталог: «Маленький бокс» переименовывается в «Маленькие ложечки» (если владелица
  его не переименовала сама), добавляются «Большие ложечки»: 1 — 1000 ₽, 2 — 1900 ₽,
  каждая следующая — 800 ₽, до 6 штук. Цены маленьких (500 / 900 / +300) уже такие;
- тексты сообщений, приветствие бота и подпись «повышенного ожидания»: фразы про бокс
  заменяются на ложечки. Заменяются только сами фразы — остальные правки владелицы
  в этих текстах сохраняются;
- палитра цветов: цвета клиент пишет текстом, таблица `colors` больше не нужна.

Revision ID: 0013_spoons_catalog
Revises: 0012_outbox_and_refunds
Create Date: 2026-11-03 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_spoons_catalog"
down_revision: str | None = "0012_outbox_and_refunds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: (ключ шаблона, было, стало)
TEMPLATE_PHRASES = (
    ("status_assembling", "Собираем ваш бокс", "Собираем ваши ложечки"),
    ("arrived_at_pickup", "Ваш бокс приехал", "Ваш заказ приехал"),
    ("status_completed", "что бокс понравился", "что ложечки понравились"),
)

#: (ключ настроек, поле, было, стало)
SETTING_PHRASES = (
    (
        "bot_texts",
        "greeting",
        "мастерская подарочных боксов Elizabet Magic",
        "мастерская Elizabet Magic",
    ),
    ("bot_texts", "greeting", "Соберём для вас бокс с ложечками", "Соберём для вас ложечки"),
    ("intake", "slow_message", "к которой соберём бокс", "к которой соберём заказ"),
)


def upgrade() -> None:
    bind = op.get_bind()

    # --- каталог ---
    op.execute(
        "UPDATE products SET name = 'Маленькие ложечки' "
        "WHERE sku = 'box-small' AND name = 'Маленький бокс'",
    )
    op.execute(
        "UPDATE products SET description = 'Маленькие ложечки ручной работы.' "
        "WHERE sku = 'box-small' AND description = 'Компактный подарочный бокс с ложечками.'",
    )
    op.execute(
        "UPDATE addons SET description = replace(description, 'именно ваш бокс', "
        "'именно ваши ложечки') WHERE description LIKE '%именно ваш бокс%'",
    )
    op.execute(
        """
        WITH big AS (
            INSERT INTO products (sku, name, description, production_days,
                                  extra_spoon_price_kopecks, max_spoon_count,
                                  sort_order, is_active)
            SELECT 'spoons-big', 'Большие ложечки', 'Большие ложечки ручной работы.', 1,
                   80000, 6, 2, true
            WHERE NOT EXISTS (SELECT 1 FROM products WHERE sku = 'spoons-big')
            RETURNING id
        )
        INSERT INTO product_variants (product_id, name, sku, spoon_count, price_kopecks,
                                      sort_order, is_active)
        SELECT big.id, v.name, v.sku, v.spoons, v.price, v.position, true
        FROM big, (VALUES ('1 ложечка', 'spoons-big-1', 1, 100000, 1),
                          ('2 ложечки', 'spoons-big-2', 2, 190000, 2))
             AS v(name, sku, spoons, price, position)
        """,
    )

    # --- тексты ---
    for key, old, new in TEMPLATE_PHRASES:
        bind.execute(
            sa.text(
                "UPDATE message_templates SET text = replace(text, CAST(:old AS text), "
                "CAST(:new AS text)) WHERE key = :key",
            ),
            {"key": key, "old": old, "new": new},
        )
    for key, field, old, new in SETTING_PHRASES:
        bind.execute(
            sa.text(
                "UPDATE settings SET value = jsonb_set(value, ARRAY[CAST(:field AS text)], "
                "to_jsonb(replace(value ->> CAST(:field AS text), CAST(:old AS text), "
                "CAST(:new AS text)))) "
                "WHERE key = :key AND value ? CAST(:field AS text)",
            ),
            {"key": key, "field": field, "old": old, "new": new},
        )

    # --- палитра ---
    op.drop_table("colors")


def downgrade() -> None:
    # Возвращается только пустая таблица цветов; переименования и «Большие ложечки»
    # остаются — это данные, их правят в панели.
    op.create_table(
        "colors",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False, start=1), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("sort_order", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_colors"),
        sa.UniqueConstraint("name", name="uq_colors_name"),
    )
