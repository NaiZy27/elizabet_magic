"""Каталог: боксы, их варианты, доп. услуги и палитра цветов."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.enums import AddonChargeMode
from core.models.base import (
    Base,
    IdMixin,
    SortableMixin,
    TimestampMixin,
    enum_type,
    money,
    non_negative,
    positive,
)

if TYPE_CHECKING:
    from core.models.orders import OrderItem


class Product(IdMixin, TimestampMixin, SortableMixin, Base):
    """Бокс. Конкретные цены и число ложечек — в вариантах."""

    __tablename__ = "products"
    __table_args__ = (
        positive("production_days"),
        non_negative("extra_spoon_price_kopecks"),
        positive("max_spoon_count"),
        # Докупка ложечек либо настроена целиком, либо выключена целиком:
        # цена без потолка означала бы заказ на сто ложечек.
        CheckConstraint(
            "(extra_spoon_price_kopecks IS NULL) = (max_spoon_count IS NULL)",
            name="extra_spoon_settings_complete",
        ),
    )

    sku: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text, default=None)
    #: file_id фотографии в Telegram. Заполняется загрузкой через админку.
    photo_file_id: Mapped[str | None] = mapped_column(String(256), default=None)

    #: Срок изготовления в рабочих днях. Обычный бокс — 1 день.
    production_days: Mapped[int] = mapped_column(Integer, default=1, server_default="1")

    #: Цена каждой ложечки сверх самого большого варианта. NULL — докупать нельзя.
    extra_spoon_price_kopecks: Mapped[int | None] = money(nullable=True, default=None)
    #: Потолок числа ложечек при докупке.
    max_spoon_count: Mapped[int | None] = mapped_column(Integer, default=None)

    variants: Mapped[list[ProductVariant]] = relationship(
        back_populates="product",
        order_by="ProductVariant.sort_order",
        lazy="selectin",
    )

    @property
    def allows_extra_spoons(self) -> bool:
        return self.extra_spoon_price_kopecks is not None and self.max_spoon_count is not None

    def __repr__(self) -> str:
        return f"<Product {self.sku} {self.name!r}>"


class ProductVariant(IdMixin, TimestampMixin, SortableMixin, Base):
    """Вариант бокса: «3 ложечки» со своей ценой, весом и габаритами."""

    __tablename__ = "product_variants"
    __table_args__ = (
        positive("spoon_count"),
        non_negative("price_kopecks"),
        positive("weight_g"),
        positive("length_cm"),
        positive("width_cm"),
        positive("height_cm"),
    )

    product_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("products.id", ondelete="RESTRICT"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(128))
    sku: Mapped[str] = mapped_column(String(64), unique=True)
    spoon_count: Mapped[int] = mapped_column(Integer)
    price_kopecks: Mapped[int] = money()

    # Нужны службе доставки для расчёта. Пока габариты упаковки не измерены,
    # поля пустые, и расчёт доставки уходит на резервную цену.
    weight_g: Mapped[int | None] = mapped_column(Integer, default=None)
    length_cm: Mapped[int | None] = mapped_column(Integer, default=None)
    width_cm: Mapped[int | None] = mapped_column(Integer, default=None)
    height_cm: Mapped[int | None] = mapped_column(Integer, default=None)

    product: Mapped[Product] = relationship(back_populates="variants")
    order_items: Mapped[list[OrderItem]] = relationship(back_populates="variant")

    @property
    def has_parcel_dimensions(self) -> bool:
        return None not in (self.weight_g, self.length_cm, self.width_cm, self.height_cm)

    def __repr__(self) -> str:
        return f"<ProductVariant {self.sku} {self.name!r}>"


class Addon(IdMixin, TimestampMixin, SortableMixin, Base):
    """Доп. услуга. Пока одна — видео сборки."""

    __tablename__ = "addons"
    __table_args__ = (
        non_negative("price_kopecks"),
        non_negative("extra_production_days"),
    )

    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text, default=None)
    price_kopecks: Mapped[int] = money()
    charge_mode: Mapped[AddonChargeMode] = mapped_column(
        enum_type(AddonChargeMode, "addon_charge_mode"),
        default=AddonChargeMode.PER_BOX,
    )
    #: Насколько услуга удлиняет сборку. Видео — +1 рабочий день.
    extra_production_days: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    def __repr__(self) -> str:
        return f"<Addon {self.code}>"


class Color(IdMixin, TimestampMixin, SortableMixin, Base):
    """Цвет из палитры: клиент отмечает любимые и нежелательные."""

    __tablename__ = "colors"

    name: Mapped[str] = mapped_column(String(64), unique=True)

    def __repr__(self) -> str:
        return f"<Color {self.name!r}>"
