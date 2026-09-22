"""Стабильные коды предметной области.

Коды в БД не меняются никогда — по ним построены CHECK-ограничения и история заказов.
Русские подписи для интерфейса лежат отдельно, в core.labels.
"""

from __future__ import annotations

from enum import StrEnum


class OrderStatus(StrEnum):
    """Этапы заказа (раздел 5.3 ТЗ)."""

    DRAFT = "draft"
    WAITING_PAYMENT = "waiting_payment"
    QUEUED = "queued"
    ASSEMBLING = "assembling"
    READY = "ready"
    SHIPPED = "shipped"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


#: Статусы, в которых заказ занимает место в общей очереди сборки.
QUEUE_STATUSES: frozenset[OrderStatus] = frozenset(
    {OrderStatus.QUEUED, OrderStatus.ASSEMBLING},
)

#: Колонки доски заказов, слева направо.
BOARD_STATUSES: tuple[OrderStatus, ...] = (
    OrderStatus.QUEUED,
    OrderStatus.ASSEMBLING,
    OrderStatus.READY,
    OrderStatus.SHIPPED,
    OrderStatus.COMPLETED,
)

#: Заказ принят в работу только после подтверждённой оплаты — эти статусы идут в метрики.
PAID_STATUSES: frozenset[OrderStatus] = frozenset(
    {
        OrderStatus.QUEUED,
        OrderStatus.ASSEMBLING,
        OrderStatus.READY,
        OrderStatus.SHIPPED,
        OrderStatus.COMPLETED,
    },
)

#: Разрешённые переходы. Всё, чего здесь нет, сервис смены статуса отклоняет.
ALLOWED_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.DRAFT: frozenset({OrderStatus.WAITING_PAYMENT, OrderStatus.CANCELLED}),
    OrderStatus.WAITING_PAYMENT: frozenset({OrderStatus.QUEUED, OrderStatus.CANCELLED}),
    OrderStatus.QUEUED: frozenset({OrderStatus.ASSEMBLING, OrderStatus.CANCELLED}),
    OrderStatus.ASSEMBLING: frozenset(
        {OrderStatus.QUEUED, OrderStatus.READY, OrderStatus.CANCELLED},
    ),
    OrderStatus.READY: frozenset(
        {OrderStatus.ASSEMBLING, OrderStatus.SHIPPED, OrderStatus.CANCELLED},
    ),
    OrderStatus.SHIPPED: frozenset(
        {OrderStatus.READY, OrderStatus.COMPLETED, OrderStatus.CANCELLED},
    ),
    OrderStatus.COMPLETED: frozenset({OrderStatus.SHIPPED}),
    OrderStatus.CANCELLED: frozenset(),
}


class CancelReason(StrEnum):
    """Почему заказ отменён. От причины зависит, можно ли его «воскресить»."""

    #: Не оплатили вовремя — если деньги всё же придут, заказ возвращается в работу.
    EXPIRED = "expired"
    CUSTOMER = "customer"
    ADMIN = "admin"
    REFUND = "refund"


class AdminRole(StrEnum):
    """Роль в панели."""

    #: Владелица: видит и меняет всё.
    OWNER = "owner"
    #: Сборщик: доска, состав и пожелания, этапы и заметки. Без денег и настроек.
    ASSEMBLER = "assembler"


class ReceiptStatus(StrEnum):
    """Чек самозанятого в «Мой налог»."""

    #: Платёж прошёл, чек ещё не зарегистрирован.
    PENDING = "pending"
    REGISTERED = "registered"
    #: Чек аннулирован — так в НПД оформляется возврат.
    ANNULLED = "annulled"
    FAILED = "failed"


class ReceiptProvider(StrEnum):
    """Кто зарегистрировал чек."""

    #: Робочеки СМЗ — Робокасса сама передаёт продажу в «Мой налог».
    ROBOKASSA = "robokassa"
    #: Внесён руками в «Мой налог», ссылка вставлена в панели.
    MANUAL = "manual"
    STUB = "stub"


class PaymentStatus(StrEnum):
    """Состояние попытки оплаты."""

    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class PaymentProvider(StrEnum):
    ROBOKASSA = "robokassa"
    #: Заглушка для локальной разработки без реальных платежей.
    STUB = "stub"


class ProviderEnvironment(StrEnum):
    """Контур внешнего сервиса. Тестовые и боевые данные не смешиваются."""

    TEST = "test"
    PRODUCTION = "production"


class DeliveryProviderCode(StrEnum):
    CDEK = "cdek"
    YANDEX = "yandex"


class ShipmentStatus(StrEnum):
    """Наш нормализованный статус отправления.

    Это не статус заказа: пока посылка едет, заказ остаётся в «Передан в доставку».
    """

    CREATED = "created"
    IN_TRANSIT = "in_transit"
    ARRIVED = "arrived"
    DELIVERED = "delivered"
    RETURNED = "returned"
    CANCELLED = "cancelled"


#: Отправления в этих статусах опрашиваются фоновой задачей.
ACTIVE_SHIPMENT_STATUSES: frozenset[ShipmentStatus] = frozenset(
    {ShipmentStatus.CREATED, ShipmentStatus.IN_TRANSIT, ShipmentStatus.ARRIVED},
)


class AddonChargeMode(StrEnum):
    """Как считается доп. услуга."""

    #: За каждый бокс в заказе.
    PER_BOX = "per_box"
    #: Один раз на весь заказ.
    PER_ORDER = "per_order"


#: Код услуги «видео сборки» — по нему ставится флаг 🎥 на карточке заказа.
VIDEO_ADDON_CODE = "video"


class OrderEventType(StrEnum):
    """Типы записей в истории заказа."""

    CREATED = "created"
    SUBMITTED = "submitted"
    PAYMENT_CREATED = "payment_created"
    PAYMENT_SUCCEEDED = "payment_succeeded"
    PAYMENT_FAILED = "payment_failed"
    STATUS_CHANGED = "status_changed"
    QUEUE_REORDERED = "queue_reordered"
    PRODUCTION_DAYS_CHANGED = "production_days_changed"
    PROMISED_DATE_CHANGED = "promised_date_changed"
    ADMIN_NOTE_CHANGED = "admin_note_changed"
    SHIPMENT_CREATED = "shipment_created"
    TRACK_ASSIGNED = "track_assigned"
    SHIPMENT_STATUS_CHANGED = "shipment_status_changed"
    DELIVERY_PRICE_FALLBACK = "delivery_price_fallback"
    EXPIRED = "expired"
    NOTIFICATION_SENT = "notification_sent"
    NOTIFICATION_FAILED = "notification_failed"
    #: Оплата пришла после автоотмены по сроку — заказ вернулся в работу.
    REVIVED = "revived"
    #: Оплата пришла по заказу, который вернуть нельзя: нужен возврат.
    PAYMENT_AFTER_CANCEL = "payment_after_cancel"
    RECEIPT_REGISTERED = "receipt_registered"
    RECEIPT_ANNULLED = "receipt_annulled"
    REFUNDED = "refunded"
    ARRIVED_AT_PICKUP = "arrived_at_pickup"


class MessageTemplateKey(StrEnum):
    """Ключи шаблонов уведомлений (раздел 5.6 ТЗ)."""

    ORDER_CREATED = "order_created"
    ORDER_PAID = "order_paid"
    STATUS_ASSEMBLING = "status_assembling"
    STATUS_READY = "status_ready"
    STATUS_SHIPPED = "status_shipped"
    TRACK_ASSIGNED = "track_assigned"
    ARRIVED_AT_PICKUP = "arrived_at_pickup"
    STATUS_COMPLETED = "status_completed"
    ORDER_CANCELLED = "order_cancelled"
    RECEIPT_ISSUED = "receipt_issued"
    ORDER_REFUNDED = "order_refunded"


#: Какой шаблон уходит клиенту при переходе в статус. Для остальных статусов — ничего.
STATUS_TEMPLATES: dict[OrderStatus, MessageTemplateKey] = {
    OrderStatus.QUEUED: MessageTemplateKey.ORDER_PAID,
    OrderStatus.ASSEMBLING: MessageTemplateKey.STATUS_ASSEMBLING,
    OrderStatus.READY: MessageTemplateKey.STATUS_READY,
    OrderStatus.SHIPPED: MessageTemplateKey.STATUS_SHIPPED,
    OrderStatus.COMPLETED: MessageTemplateKey.STATUS_COMPLETED,
    OrderStatus.CANCELLED: MessageTemplateKey.ORDER_CANCELLED,
}
