"""Заказы: сам заказ, его состав, доп. услуги и история.

Состав и цены хранятся снимком на момент заказа: правка каталога не должна
задним числом менять то, что клиент уже оплатил.

В заказе может быть несколько боксов. У каждого бокса свои пожелания и свои
услуги (видео снимается на конкретный бокс), а пункт выдачи и чек — общие.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Sequence,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.enums import (
    VIDEO_ADDON_CODE,
    AddonChargeMode,
    CancelReason,
    DeliveryProviderCode,
    OrderEventType,
    OrderStatus,
)
from core.models.base import (
    Base,
    IdMixin,
    TimestampMixin,
    enum_type,
    money,
    non_negative,
    positive,
)
from core.money import format_rubles

if TYPE_CHECKING:
    from core.models.catalog import Addon, ProductVariant
    from core.models.customers import Customer
    from core.models.delivery import PickupPoint, Shipment
    from core.models.payments import Payment, Receipt

#: Номера заказов идут отдельной последовательностью, а не от id, и выдаются только
#: при оплате: клиенту показываем «EM-1001», и по номерам не должно быть видно,
#: сколько заказов бросили на этапе оплаты.
ORDER_NUMBER_PREFIX = "EM-"
order_number_seq = Sequence("order_number_seq", start=1001, metadata=Base.metadata)


class Order(IdMixin, TimestampMixin, Base):
    __tablename__ = "orders"
    __table_args__ = (
        non_negative("subtotal_kopecks"),
        non_negative("delivery_kopecks"),
        non_negative("total_kopecks"),
        CheckConstraint(
            "total_kopecks = subtotal_kopecks + delivery_kopecks",
            name="total_matches_parts",
        ),
        positive("production_days"),
        # Место в очереди есть только у заказов, которые ждут сборки или собираются.
        CheckConstraint(
            "queue_position IS NULL OR (queue_position > 0 AND status IN ('queued', 'assembling'))",
            name="queue_position_only_in_queue",
        ),
        # Перестановка карточек на доске на мгновение создаёт одинаковые позиции,
        # поэтому уникальность проверяется в конце транзакции.
        UniqueConstraint(
            "queue_position",
            name="uq_orders_queue_position",
            deferrable=True,
            initially="DEFERRED",
        ),
        # У клиента одновременно не больше одного незавершённого оформления.
        Index(
            "uq_orders_single_draft",
            "customer_id",
            unique=True,
            postgresql_where=text("status = 'draft'"),
        ),
        Index("ix_orders_customer_created", "customer_id", "created_at"),
        Index("ix_orders_status_created", "status", "created_at"),
    )

    #: NULL до оплаты: номер присваивается вместе с подтверждением платежа.
    number: Mapped[str | None] = mapped_column(String(16), unique=True, default=None)

    customer_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("customers.id", ondelete="RESTRICT"),
    )
    status: Mapped[OrderStatus] = mapped_column(
        enum_type(OrderStatus, "order_status"),
        default=OrderStatus.DRAFT,
    )
    #: Когда заказ последний раз менял этап — по этому полю карточки сортируются
    #: в колонках доски, где ручного порядка нет.
    status_changed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    # Получатель для службы доставки: снимок, а не ссылка на карточку клиента.
    recipient_name: Mapped[str | None] = mapped_column(String(256), default=None)
    recipient_phone: Mapped[str | None] = mapped_column(String(32), default=None)
    recipient_email: Mapped[str | None] = mapped_column(String(256), default=None)

    subtotal_kopecks: Mapped[int] = money(default=0, server_default="0")
    delivery_kopecks: Mapped[int] = money(default=0, server_default="0")
    total_kopecks: Mapped[int] = money(default=0, server_default="0")

    #: Внутренняя заметка для сборки. Клиент её не видит.
    admin_note: Mapped[str | None] = mapped_column(Text, default=None)

    #: Срок изготовления в рабочих днях. Считается при создании, правится в карточке.
    production_days: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    #: Место в общей очереди сборки, сквозное 1..N.
    queue_position: Mapped[int | None] = mapped_column(BigInteger, default=None)
    #: Дата, которую назвали клиенту при оплате. Сама не пересчитывается.
    promised_ready_date: Mapped[dt.date | None] = mapped_column(Date, default=None)

    delivery_provider: Mapped[DeliveryProviderCode | None] = mapped_column(
        enum_type(DeliveryProviderCode, "order_delivery_provider"),
        default=None,
    )
    pickup_point_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("pickup_points.id", ondelete="RESTRICT"),
        default=None,
        index=True,
    )
    #: Снимок ПВЗ на момент заказа: справочник меняется, заказ — нет.
    pickup_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)

    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    paid_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    ready_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    #: Посылка прибыла в пункт выдачи: отмечается руками или синхронизацией со службой.
    arrived_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    cancelled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    cancel_reason: Mapped[CancelReason | None] = mapped_column(
        enum_type(CancelReason, "order_cancel_reason"),
        default=None,
    )
    #: Деньги вернули клиенту. Такой заказ не входит в выручку.
    refunded_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    #: Сообщения бота с кнопками по этому заказу: [{"chat_id", "message_id", "text"?}].
    #: Когда заказ оплачен или отменён, кнопки у них снимаются, а под текстом
    #: дописывается итог — редактировать сообщения Telegram разрешает без срока.
    bot_messages: Mapped[list[dict[str, int | str]]] = mapped_column(
        JSONB,
        default=list,
        server_default="[]",
    )

    customer: Mapped[Customer] = relationship(back_populates="orders")
    pickup_point: Mapped[PickupPoint | None] = relationship()
    items: Mapped[list[OrderItem]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="OrderItem.id",
    )
    addons: Mapped[list[OrderAddon]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    events: Mapped[list[OrderEvent]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="OrderEvent.created_at.desc()",
    )
    payments: Mapped[list[Payment]] = relationship(
        back_populates="order",
        order_by="Payment.created_at.desc()",
    )
    shipments: Mapped[list[Shipment]] = relationship(
        back_populates="order",
        order_by="Shipment.created_at.desc()",
    )
    receipts: Mapped[list[Receipt]] = relationship(
        back_populates="order",
        order_by="Receipt.created_at.desc()",
    )

    @property
    def display_number(self) -> str:
        """Как назвать заказ клиенту: «EM-1025», а до оплаты — «на 1 700 ₽».

        Читается в любой фразе: «Заказ EM-1025 принят», «Заказ на 1 700 ₽ оформлен».
        """
        return self.number or f"на {format_rubles(self.total_kopecks)}"

    @property
    def admin_label(self) -> str:
        """Обозначение в панели: у неоплаченного номера нет, показываем внутренний id."""
        return self.number or f"б/н #{self.id}"

    @property
    def is_paid(self) -> bool:
        return self.paid_at is not None

    @property
    def box_count(self) -> int:
        return sum(item.quantity for item in self.items)

    @property
    def order_addons(self) -> list[OrderAddon]:
        """Услуги на весь заказ — те, что не привязаны ни к одному боксу."""
        return [
            addon for addon in self.addons if addon.order_item_id is None and addon.item is None
        ]

    @property
    def video_box_count(self) -> int:
        """Сколько боксов нужно снять на видео."""
        return sum(item.quantity for item in self.items if item.has_video)

    @property
    def has_wishes(self) -> bool:
        return any(item.has_wishes for item in self.items)

    def __repr__(self) -> str:
        return f"<Order {self.number or f'draft#{self.id}'} {self.status}>"


class OrderItem(IdMixin, TimestampMixin, Base):
    """Бокс в заказе: комплектация, цена и пожелания именно к нему.

    Бот заводит каждый бокс отдельной позицией с quantity = 1, потому что пожелания
    у боксов разные. Поле quantity оставлено для будущих одинаковых наборов.
    """

    __tablename__ = "order_items"
    __table_args__ = (
        positive("quantity"),
        positive("spoon_count"),
        non_negative("unit_price_kopecks"),
        Index("ix_order_items_order", "order_id"),
    )

    order_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("orders.id", ondelete="CASCADE"),
    )
    variant_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("product_variants.id", ondelete="RESTRICT"),
    )
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    #: Может быть больше, чем в варианте, если клиент докупил ложечки.
    spoon_count: Mapped[int] = mapped_column(Integer)
    unit_price_kopecks: Mapped[int] = money()
    name_snapshot: Mapped[str] = mapped_column(String(256))

    # Пожелания к этому боксу. Цвета — названиями на момент заказа: палитру правят
    # в админке, а сборщик должен увидеть то, что выбрал клиент.
    favorite_colors: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    avoid_colors: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    comment: Mapped[str | None] = mapped_column(Text, default=None)

    order: Mapped[Order] = relationship(back_populates="items")
    variant: Mapped[ProductVariant] = relationship(back_populates="order_items")
    addons: Mapped[list[OrderAddon]] = relationship(
        back_populates="item",
        lazy="selectin",
        order_by="OrderAddon.id",
        # Удаляются вместе с заказом через Order.addons; здесь только чтение и привязка.
        passive_deletes=True,
    )

    @property
    def total_kopecks(self) -> int:
        """Бокс вместе с его услугами."""
        return self.unit_price_kopecks * self.quantity + sum(
            addon.total_kopecks for addon in self.addons
        )

    @property
    def has_video(self) -> bool:
        return any(addon.code_snapshot == VIDEO_ADDON_CODE for addon in self.addons)

    @property
    def has_wishes(self) -> bool:
        return bool(self.favorite_colors or self.avoid_colors or self.comment)

    def __repr__(self) -> str:
        return f"<OrderItem {self.name_snapshot!r} x{self.quantity}>"


class OrderAddon(IdMixin, TimestampMixin, Base):
    """Доп. услуга в заказе.

    Услуга «за каждый бокс» привязана к своему боксу (order_item_id), услуга
    «один раз на заказ» — только к заказу.
    """

    __tablename__ = "order_addons"
    __table_args__ = (
        # Одна и та же услуга не может дважды висеть на одном боксе…
        Index(
            "uq_order_addons_item_addon",
            "order_item_id",
            "addon_id",
            unique=True,
            postgresql_where=text("order_item_id IS NOT NULL"),
        ),
        # …и дважды на заказе целиком.
        Index(
            "uq_order_addons_order_addon",
            "order_id",
            "addon_id",
            unique=True,
            postgresql_where=text("order_item_id IS NULL"),
        ),
        positive("quantity"),
        non_negative("unit_price_kopecks"),
    )

    order_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("orders.id", ondelete="CASCADE"),
    )
    order_item_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("order_items.id", ondelete="CASCADE"),
        default=None,
        index=True,
    )
    addon_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("addons.id", ondelete="RESTRICT"),
    )
    #: Для услуги «за каждый бокс» — количество этого бокса, иначе 1.
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    unit_price_kopecks: Mapped[int] = money()
    name_snapshot: Mapped[str] = mapped_column(String(128))
    #: Код услуги на момент заказа: по нему ставится флаг «🎥 видео» без обращения
    #: к справочнику, который мог измениться.
    code_snapshot: Mapped[str] = mapped_column(String(32))
    charge_mode_snapshot: Mapped[AddonChargeMode] = mapped_column(
        enum_type(AddonChargeMode, "order_addon_charge_mode"),
    )

    order: Mapped[Order] = relationship(back_populates="addons")
    item: Mapped[OrderItem | None] = relationship(back_populates="addons")
    addon: Mapped[Addon] = relationship()

    @property
    def total_kopecks(self) -> int:
        return self.unit_price_kopecks * self.quantity

    def __repr__(self) -> str:
        return f"<OrderAddon {self.name_snapshot!r} x{self.quantity}>"


class OrderEvent(IdMixin, Base):
    """Запись в истории заказа. Пишется на каждое значимое действие."""

    __tablename__ = "order_events"
    __table_args__ = (Index("ix_order_events_order_created", "order_id", "created_at"),)

    order_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("orders.id", ondelete="CASCADE"),
    )
    event_type: Mapped[OrderEventType] = mapped_column(
        enum_type(OrderEventType, "order_event_type", length=48),
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    #: Кто сделал: «bot», «admin:<id>» или «system».
    actor: Mapped[str] = mapped_column(String(32), default="system")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    order: Mapped[Order] = relationship(back_populates="events")

    def __repr__(self) -> str:
        return f"<OrderEvent {self.event_type} order={self.order_id}>"
