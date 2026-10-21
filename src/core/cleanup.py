"""Очистка базы от тестовых покупателей и заказов перед запуском.

    python -m core.cleanup            — показать, что будет удалено (ничего не меняет)
    python -m core.cleanup --apply    — удалить

Удаляются покупатели, их заказы со всей историей, оплаты и чеки — кроме заказов
с настоящим чеком: оплата прошла в боевом режиме Робокассы, чек ушёл в «Мой налог»,
и хранить его мы обязаны по закону. Такие заказы остаются целиком вместе с покупателем,
только у них забываются сообщения бота: после смены токена они принадлежат чужому боту.

Каталог, цены, настройки, тексты, службы доставки, пункты выдачи и логины панели
не трогаются. Сбрасываются состояния диалогов в боте (Redis), а если заказов
не осталось — и нумерация заказов (снова с EM-1001).

Всё удаление — одна транзакция: при ошибке база остаётся как была.
"""

from __future__ import annotations

import argparse
import asyncio

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from core.db import dispose_engine, session_scope
from core.enums import ReceiptProvider

#: Заказы с настоящим чеком — их не трогаем.
_KEPT = f"SELECT order_id FROM receipts WHERE provider != '{ReceiptProvider.STUB}'"

#: (что удаляется, запрос) — по порядку: сначала то, что ссылается на заказы.
_DELETES = (
    ("чеки", f"DELETE FROM receipts WHERE order_id NOT IN ({_KEPT})"),
    ("оплаты", f"DELETE FROM payments WHERE order_id NOT IN ({_KEPT})"),
    ("отправления", f"DELETE FROM shipments WHERE order_id NOT IN ({_KEPT})"),
    # Позиции, услуги и история заказа удаляются вместе с ним (ON DELETE CASCADE).
    ("заказы", f"DELETE FROM orders WHERE id NOT IN ({_KEPT})"),
    (
        "покупатели",
        "DELETE FROM customers WHERE id NOT IN (SELECT customer_id FROM orders)",
    ),
)


async def _counts(session: AsyncSession) -> dict[str, int]:
    """Сколько строк удалит каждый шаг, если запустить его сейчас."""
    counts = {}
    for name, delete in _DELETES:
        if name == "покупатели":
            query = (
                "SELECT count(*) FROM customers WHERE id NOT IN "
                f"(SELECT customer_id FROM orders WHERE id IN ({_KEPT}))"
            )
        else:
            query = delete.replace("DELETE FROM", "SELECT count(*) FROM", 1)
        counts[name] = await session.scalar(text(query)) or 0
    return counts


async def _clear_bot_states() -> int:
    """Сбросить состояния диалогов: иначе бот продолжит оформлять удалённый черновик."""
    redis = Redis.from_url(get_settings().redis_fsm_url, decode_responses=True)
    try:
        keys = [key async for key in redis.scan_iter(match="fsm:*")]
        if keys:
            await redis.delete(*keys)
        return len(keys)
    finally:
        await redis.aclose()


async def run(*, apply: bool) -> None:
    async with session_scope() as session:
        counts = await _counts(session)
        kept = await session.scalar(text(f"SELECT count(DISTINCT order_id) FROM ({_KEPT}) AS k"))
        print("Будет удалено:" if not apply else "Удаляю:")
        for name, count in counts.items():
            print(f"  {name}: {count}")
        print(f"Остаются заказы с настоящим чеком: {kept or 0}")
        if not apply:
            print("\nНичего не изменено. Чтобы удалить, запустите с --apply.")
            return

        for _, delete in _DELETES:
            await session.execute(text(delete))
        # Оставшиеся заказы: сообщения тестового бота основному боту не принадлежат.
        await session.execute(text("UPDATE orders SET bot_messages = '[]'"))
        if not kept:
            await session.execute(text("ALTER SEQUENCE order_number_seq RESTART WITH 1001"))
            print("Нумерация заказов начнётся заново с EM-1001.")

    states = await _clear_bot_states()
    print(f"Сброшено состояний диалогов в боте: {states}")
    print("Готово.")


async def _main(*, apply: bool) -> None:
    try:
        await run(apply=apply)
    finally:
        await dispose_engine()


def main() -> None:
    parser = argparse.ArgumentParser(description="Удалить тестовых покупателей и заказы")
    parser.add_argument(
        "--apply", action="store_true", help="удалить (без флага — только показать)"
    )
    args = parser.parse_args()
    asyncio.run(_main(apply=args.apply))


if __name__ == "__main__":
    main()
