"""Доставка: службы, кэш пунктов выдачи и отправления."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.enums import DeliveryProviderCode, ProviderEnvironment, ShipmentStatus
from core.models.base import (
    Base,
    IdMixin,
    TimestampMixin,
    enum_type,
    money,
    non_negative,
    positive,
)
from core.text import normalize_search

if TYPE_CHECKING:
    from core.models.orders import Order


class DeliveryProvider(IdMixin, TimestampMixin, Base):
    """Служба доставки и её настройки. Включается и выключается из админки."""

    __tablename__ = "delivery_providers"
    __table_args__ = (non_negative("fallback_price_kopecks"),)

    code: Mapped[DeliveryProviderCode] = mapped_column(
        enum_type(DeliveryProviderCode, "delivery_provider_code"),
        unique=True,
    )
    name: Mapped[str] = mapped_column(String(64))
    is_enabled: Mapped[bool] = mapped_column(default=False, server_default="false")
    #: Цена, по которой считаем доставку, если API службы недоступно.
    fallback_price_kopecks: Mapped[int] = money(server_default="30000")
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    #: Когда последний раз успешно обновился справочник ПВЗ.
    pickup_points_synced_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        default=None,
    )
    last_sync_error: Mapped[str | None] = mapped_column(Text, default=None)

    def __repr__(self) -> str:
        return f"<DeliveryProvider {self.code}>"


class PickupPoint(IdMixin, TimestampMixin, Base):
    """Пункт выдачи. Кэш справочника службы, обновляется по расписанию.

    Точки не удаляются: на них ссылаются оформленные заказы. Пропавшие из ответа
    API просто выключаются.
    """

    __tablename__ = "pickup_points"
    __table_args__ = (
        UniqueConstraint("provider", "environment", "external_id"),
        CheckConstraint("latitude BETWEEN -90 AND 90", name="latitude_range"),
        CheckConstraint("longitude BETWEEN -180 AND 180", name="longitude_range"),
        positive("max_weight_g"),
        # Поиск ближайших: сначала отсекаем прямоугольником по широте.
        Index(
            "ix_pickup_points_geo",
            "provider",
            "environment",
            "is_active",
            "latitude",
        ),
        Index("ix_pickup_points_city", "city"),
    )

    provider: Mapped[DeliveryProviderCode] = mapped_column(
        enum_type(DeliveryProviderCode, "pickup_point_provider"),
    )
    environment: Mapped[ProviderEnvironment] = mapped_column(
        enum_type(ProviderEnvironment, "pickup_point_environment"),
        default=ProviderEnvironment.TEST,
    )
    #: Код точки у службы: code у СДЭК, id у Яндекса.
    external_id: Mapped[str] = mapped_column(String(128))

    name: Mapped[str] = mapped_column(String(256))
    city: Mapped[str] = mapped_column(String(128))
    #: Код города у службы: city_code у СДЭК, geo_id у Яндекса. Нужен для расчёта цены.
    city_code: Mapped[str | None] = mapped_column(String(64), default=None)
    address: Mapped[str] = mapped_column(String(512))
    latitude: Mapped[float] = mapped_column(Double)
    longitude: Mapped[float] = mapped_column(Double)
    working_hours: Mapped[str | None] = mapped_column(String(256), default=None)

    # Ограничения точки, если служба их отдаёт: не показываем ПВЗ, куда бокс не примут.
    max_weight_g: Mapped[int | None] = mapped_column(Integer, default=None)
    max_dimensions_cm: Mapped[str | None] = mapped_column(String(64), default=None)

    is_active: Mapped[bool] = mapped_column(default=True, server_default="true")
    synced_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    #: Город, адрес и название в виде для поиска (см. core.text.normalize_search).
    #: Заполняется само при каждой записи — руками не трогать.
    search_text: Mapped[str] = mapped_column(Text, default="", server_default="")

    def refresh_search_text(self) -> None:
        self.search_text = normalize_search(f"{self.city} {self.address} {self.name}")

    def __repr__(self) -> str:
        return f"<PickupPoint {self.provider}:{self.external_id} {self.city}>"


@event.listens_for(PickupPoint, "before_insert")
@event.listens_for(PickupPoint, "before_update")
def _fill_search_text(_mapper, _connection, point: PickupPoint) -> None:
    point.refresh_search_text()


class Shipment(IdMixin, TimestampMixin, Base):
    """Отправление у службы доставки.

    Этапы доставки живут здесь и не меняют статус заказа: пока посылка едет,
    заказ остаётся в колонке «Передан в доставку».
    """

    __tablename__ = "shipments"
    __table_args__ = (
        UniqueConstraint("provider", "external_id"),
        # Одно живое отправление на заказ; отменённые не мешают создать новое.
        Index(
            "uq_shipments_active_order",
            "order_id",
            unique=True,
            postgresql_where=text("status <> 'cancelled'"),
        ),
        Index("ix_shipments_track_number", "track_number"),
    )

    # RESTRICT, а не CASCADE: заказы не удаляются, а история отправлений нужна
    # для разбора спорных ситуаций со службой.
    order_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("orders.id", ondelete="RESTRICT"),
        index=True,
    )
    provider: Mapped[DeliveryProviderCode] = mapped_column(
        enum_type(DeliveryProviderCode, "shipment_provider"),
    )
    #: uuid заказа у СДЭК / request_id у Яндекса.
    external_id: Mapped[str] = mapped_column(String(128))
    #: Номер для отслеживания. У СДЭК приходит асинхронно, поэтому не сразу.
    track_number: Mapped[str | None] = mapped_column(String(64), default=None)

    status: Mapped[ShipmentStatus] = mapped_column(
        enum_type(ShipmentStatus, "shipment_status"),
        default=ShipmentStatus.CREATED,
    )
    #: Статус словами службы — на случай, если маппинг чего-то не учёл.
    status_raw: Mapped[str | None] = mapped_column(String(128), default=None)
    status_updated_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        default=None,
    )
    label_url: Mapped[str | None] = mapped_column(String(512), default=None)

    order: Mapped[Order] = relationship(back_populates="shipments")

    def __repr__(self) -> str:
        return f"<Shipment {self.provider}:{self.external_id} {self.status}>"
