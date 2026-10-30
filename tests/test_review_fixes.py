"""Исправления после код-ревью: этапы без оплаты, экранирование, очередь сообщений,
дубли пунктов выдачи, пульс процессов — без базы."""

from __future__ import annotations

import contextlib
import datetime as dt

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import User

from bot.middlewares import context
from core import health
from core.enums import MessageTemplateKey, OrderStatus
from core.errors import ValidationError
from core.models import Order, OrderAddon, OrderItem
from core.services import notifications, outbox, pickup_sync
from core.services.orders import change_status, manual_targets
from web.security import is_safe_next


def _order(status: OrderStatus, *, paid: bool) -> Order:
    order = Order(
        id=7,
        status=status,
        paid_at=dt.datetime(2026, 10, 1, tzinfo=dt.UTC) if paid else None,
        subtotal_kopecks=90000,
        delivery_kopecks=30000,
        total_kopecks=120000,
        recipient_name="Анна <Иванова>",
        recipient_email="anna@mail.ru",
        pickup_snapshot={
            "provider": "ozon",
            "provider_name": "Ozon",
            "address": "ул. Мира, 5 <вход со двора> & кафе",
            "manual": True,
        },
    )
    item = OrderItem(name_snapshot="Бокс «Чай & сладости»", quantity=1, unit_price_kopecks=90000)
    item.addons = [OrderAddon(name_snapshot="Видео <сборки>", quantity=1, unit_price_kopecks=0)]
    order.items = [item]
    order.addons = list(item.addons)
    return order


# --- в работу заказ попадает только с оплатой ---


def test_unpaid_order_cannot_be_queued_by_hand():
    order = _order(OrderStatus.WAITING_PAYMENT, paid=False)
    assert manual_targets(order) == frozenset({OrderStatus.CANCELLED})


async def test_change_status_refuses_unpaid_to_queue():
    order = _order(OrderStatus.WAITING_PAYMENT, paid=False)
    # Проверка срабатывает до обращения к базе — сессия не нужна.
    with pytest.raises(ValidationError, match="не оплачен"):
        await change_status(None, order, OrderStatus.QUEUED)  # type: ignore[arg-type]


def test_paid_order_moves_freely():
    order = _order(OrderStatus.QUEUED, paid=True)
    assert manual_targets(order) == frozenset({OrderStatus.ASSEMBLING, OrderStatus.CANCELLED})


# --- экранирование в сообщениях Telegram ---


def test_customer_values_are_escaped_in_context():
    context = notifications.build_context(_order(OrderStatus.QUEUED, paid=True))

    assert context["pickup_address"] == "ул. Мира, 5 &lt;вход со двора&gt; &amp; кафе"
    assert context["customer_name"] == "Анна &lt;Иванова&gt;"
    # Состав экранирован один раз, а не дважды.
    assert "Чай &amp; сладости" in context["items"]
    assert "видео &lt;сборки&gt;" in context["items"]
    assert "&amp;amp;" not in context["items"]


def test_template_tags_survive_rendering():
    order = _order(OrderStatus.QUEUED, paid=True)
    text = notifications.render("<b>{{ pickup_address }}</b>", notifications.build_context(order))
    assert text == "<b>ул. Мира, 5 &lt;вход со двора&gt; &amp; кафе</b>"


# --- очередь сообщений ---


def test_customer_notification_keeps_values_of_the_moment():
    class _Session:
        def __init__(self) -> None:
            self.info: dict = {}
            self.added: list = []

        def add(self, item) -> None:
            self.added.append(item)

    session = _Session()
    order = _order(OrderStatus.QUEUED, paid=True)
    notifications.schedule_customer_notification(session, order, MessageTemplateKey.ORDER_PAID)
    order.pickup_snapshot = {"address": "другой адрес"}

    # Адрес поменяли после события — в очереди остался тот, что был в момент события.
    context = session.added[0].payload["context"]
    assert context["pickup_address"] == "ул. Мира, 5 &lt;вход со двора&gt; &amp; кафе"


def test_outbox_retry_grows_and_is_capped():
    assert outbox.retry_delay(1) == dt.timedelta(seconds=30)
    assert outbox.retry_delay(2) == dt.timedelta(minutes=1)
    assert outbox.retry_delay(3) == dt.timedelta(minutes=2)
    assert outbox.retry_delay(20) == outbox.RETRY_MAX


# --- пункты выдачи ---


def test_duplicate_points_are_merged():
    def row(external_id: str, address: str) -> pickup_sync.PointRow:
        return pickup_sync.PointRow(
            external_id=external_id,
            name="Пункт выдачи Ozon",
            city="Москва",
            address=address,
            latitude=55.7,
            longitude=37.6,
        )

    rows = pickup_sync.unique_rows([row("n1", "старый"), row("n2", "другой"), row("n1", "новый")])
    assert [(item.external_id, item.address) for item in rows] == [
        ("n1", "новый"),
        ("n2", "другой"),
    ]


# --- вход в панель ---


@pytest.mark.parametrize(
    ("value", "safe"),
    [("/admin/orders/5", True), ("//evil.example", False), ("/\\evil.example", False)],
)
def test_next_url_stays_on_site(value, safe):
    assert is_safe_next(value) is safe


# --- сбой Telegram не откатывает действие клиента ---


@pytest.mark.parametrize(
    ("error", "committed"),
    [(TelegramNetworkError(None, "timeout"), True), (RuntimeError("баг"), False)],  # type: ignore[arg-type]
)
async def test_telegram_outage_keeps_update(monkeypatch, error, committed):
    outcome: list[str] = []

    @contextlib.asynccontextmanager
    async def fake_scope():
        try:
            yield object()
        except Exception:
            outcome.append("rollback")
            raise
        outcome.append("commit")

    async def fake_customer(session, **kwargs):
        return object()

    monkeypatch.setattr(context, "session_scope", fake_scope)
    monkeypatch.setattr(context.orders, "get_or_create_customer", fake_customer)

    async def handler(event, data):
        raise error

    user = User(id=555, is_bot=False, first_name="Анна")
    middleware = context.ContextMiddleware()
    if committed:
        assert await middleware(handler, None, {"event_from_user": user}) is None
    else:
        with pytest.raises(RuntimeError):
            await middleware(handler, None, {"event_from_user": user})
    assert outcome == ["commit" if committed else "rollback"]


# --- пульс для healthcheck ---


def test_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "HEARTBEAT_DIR", tmp_path)
    assert health.main(["beat", "bot", "120"]) == 1

    health.beat("bot")
    assert health.main(["beat", "bot", "120"]) == 0
    assert health.main(["beat", "bot", "-1"]) == 1
