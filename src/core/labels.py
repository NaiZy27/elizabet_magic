"""Русские подписи к кодам. Единственное место, где коды превращаются в текст для людей.

В интерфейсе (бот, админка, уведомления) не должно быть ни одного сырого кода
вроде `cdek` или `waiting_payment`.
"""

from __future__ import annotations

from core.enums import (
    AddonChargeMode,
    AdminRole,
    CancelReason,
    DeliveryProviderCode,
    MessageTemplateKey,
    OrderEventType,
    OrderStatus,
    PaymentProvider,
    PaymentStatus,
    ReceiptProvider,
    ReceiptStatus,
    ShipmentStatus,
)

ORDER_STATUS_LABELS: dict[OrderStatus, str] = {
    OrderStatus.DRAFT: "Черновик",
    OrderStatus.WAITING_PAYMENT: "Ожидает оплаты",
    OrderStatus.QUEUED: "В очереди",
    OrderStatus.ASSEMBLING: "Собирается",
    OrderStatus.READY: "Готов",
    OrderStatus.SHIPPED: "Передан в доставку",
    OrderStatus.ARRIVED: "Доставлен в ПВЗ",
    OrderStatus.COMPLETED: "Получен",
    OrderStatus.CANCELLED: "Отменён",
}

PAYMENT_STATUS_LABELS: dict[PaymentStatus, str] = {
    PaymentStatus.PENDING: "Ожидает оплаты",
    PaymentStatus.PAID: "Оплачен",
    PaymentStatus.FAILED: "Не прошёл",
    PaymentStatus.CANCELLED: "Отменён",
    PaymentStatus.REFUNDED: "Возвращён",
}

PAYMENT_PROVIDER_LABELS: dict[PaymentProvider, str] = {
    PaymentProvider.ROBOKASSA: "Робокасса",
    PaymentProvider.STUB: "Тестовая оплата",
}

CANCEL_REASON_LABELS: dict[CancelReason, str] = {
    CancelReason.EXPIRED: "не оплачен вовремя",
    CancelReason.CUSTOMER: "отменён клиентом",
    CancelReason.ADMIN: "отменён в панели",
    CancelReason.REFUND: "возврат оплаты",
}

ADMIN_ROLE_LABELS: dict[AdminRole, str] = {
    AdminRole.OWNER: "Владелица",
    AdminRole.ASSEMBLER: "Сборщик",
}

RECEIPT_STATUS_LABELS: dict[ReceiptStatus, str] = {
    ReceiptStatus.PENDING: "Ожидает регистрации",
    ReceiptStatus.REGISTERED: "Зарегистрирован",
    ReceiptStatus.ANNULLED: "Аннулирован",
    ReceiptStatus.FAILED: "Ошибка регистрации",
}

RECEIPT_PROVIDER_LABELS: dict[ReceiptProvider, str] = {
    ReceiptProvider.ROBOKASSA: "Робочеки",
    ReceiptProvider.MANUAL: "Вручную в «Мой налог»",
    ReceiptProvider.STUB: "Тестовый",
}

SHIPMENT_STATUS_LABELS: dict[ShipmentStatus, str] = {
    ShipmentStatus.CREATED: "Отправление создано",
    ShipmentStatus.IN_TRANSIT: "В пути",
    ShipmentStatus.ARRIVED: "Прибыл в пункт выдачи",
    ShipmentStatus.DELIVERED: "Получен",
    ShipmentStatus.RETURNED: "Возвращается отправителю",
    ShipmentStatus.CANCELLED: "Отправление отменено",
}

DELIVERY_PROVIDER_LABELS: dict[DeliveryProviderCode, str] = {
    DeliveryProviderCode.CDEK: "СДЭК",
    DeliveryProviderCode.YANDEX: "Яндекс Доставка",
}

ADDON_CHARGE_MODE_LABELS: dict[AddonChargeMode, str] = {
    AddonChargeMode.PER_BOX: "За каждый бокс",
    AddonChargeMode.PER_ORDER: "Один раз на заказ",
}

ORDER_EVENT_LABELS: dict[OrderEventType, str] = {
    OrderEventType.CREATED: "Заказ создан",
    OrderEventType.SUBMITTED: "Заказ оформлен, ожидает оплаты",
    OrderEventType.PAYMENT_CREATED: "Создана ссылка на оплату",
    OrderEventType.PAYMENT_SUCCEEDED: "Оплата получена",
    OrderEventType.PAYMENT_FAILED: "Оплата не прошла",
    OrderEventType.STATUS_CHANGED: "Этап изменён",
    OrderEventType.QUEUE_REORDERED: "Изменено место в очереди",
    OrderEventType.PRODUCTION_DAYS_CHANGED: "Изменён срок изготовления",
    OrderEventType.PROMISED_DATE_CHANGED: "Изменена обещанная дата",
    OrderEventType.ADMIN_NOTE_CHANGED: "Изменена заметка для сборки",
    OrderEventType.SHIPMENT_CREATED: "Создано отправление",
    OrderEventType.TRACK_ASSIGNED: "Присвоен трек-номер",
    OrderEventType.SHIPMENT_STATUS_CHANGED: "Изменён этап доставки",
    OrderEventType.DELIVERY_PRICE_FALLBACK: "Доставка посчитана по резервной цене",
    OrderEventType.EXPIRED: "Истёк срок оплаты",
    OrderEventType.NOTIFICATION_SENT: "Отправлено уведомление клиенту",
    OrderEventType.NOTIFICATION_FAILED: "Уведомление не доставлено",
    OrderEventType.REVIVED: "Оплата пришла после автоотмены — заказ возвращён",
    OrderEventType.PAYMENT_AFTER_CANCEL: "⚠️ Оплата по отменённому заказу — нужен возврат",
    OrderEventType.RECEIPT_REGISTERED: "Чек зарегистрирован",
    OrderEventType.RECEIPT_ANNULLED: "Чек аннулирован",
    OrderEventType.REFUNDED: "Оплата возвращена",
    OrderEventType.ARRIVED_AT_PICKUP: "Посылка в пункте выдачи",
}

#: Названия шаблонов уведомлений в админке: что это и когда уходит клиенту.
MESSAGE_TEMPLATE_LABELS: dict[MessageTemplateKey, tuple[str, str]] = {
    MessageTemplateKey.ORDER_CREATED: (
        "Заказ оформлен",
        "Отправляется сразу после оформления, вместе с кнопкой оплаты.",
    ),
    MessageTemplateKey.ORDER_PAID: (
        "Оплата получена",
        "Подтверждение оплаты: заказу присвоен номер, он принят в работу.",
    ),
    MessageTemplateKey.RECEIPT_ISSUED: (
        "Чек сформирован",
        "Когда чек зарегистрирован в «Мой налог» и у него есть ссылка.",
    ),
    MessageTemplateKey.ORDER_REFUNDED: (
        "Оплата возвращена",
        "Когда вы оформили возврат по заказу.",
    ),
    MessageTemplateKey.STATUS_ASSEMBLING: (
        "Заказ собирается",
        "Когда вы переносите заказ в колонку «Собирается».",
    ),
    MessageTemplateKey.STATUS_READY: (
        "Заказ готов",
        "Когда вы переносите заказ в колонку «Готов».",
    ),
    MessageTemplateKey.STATUS_SHIPPED: (
        "Передан в доставку",
        "Когда вы переносите заказ в колонку «Передан в доставку».",
    ),
    MessageTemplateKey.TRACK_ASSIGNED: (
        "Трек-номер присвоен",
        "Когда служба доставки выдала номер для отслеживания.",
    ),
    MessageTemplateKey.ARRIVED_AT_PICKUP: (
        "Посылка в пункте выдачи",
        "Когда заказ переходит в колонку «Доставлен в ПВЗ» — вручную "
        "или по данным службы доставки.",
    ),
    MessageTemplateKey.STATUS_COMPLETED: (
        "Спасибо за заказ",
        "Когда заказ отмечен полученным — вами на доске или службой доставки.",
    ),
    MessageTemplateKey.ORDER_CANCELLED: (
        "Заказ отменён",
        "При отмене заказа вами или автоматически по истечении срока оплаты.",
    ),
}


def order_status_label(status: str) -> str:
    """Подпись этапа заказа. Неизвестный код показываем как есть, а не роняем страницу."""
    try:
        return ORDER_STATUS_LABELS[OrderStatus(status)]
    except ValueError:
        return status


def payment_status_label(status: str) -> str:
    try:
        return PAYMENT_STATUS_LABELS[PaymentStatus(status)]
    except ValueError:
        return status


def shipment_status_label(status: str) -> str:
    try:
        return SHIPMENT_STATUS_LABELS[ShipmentStatus(status)]
    except ValueError:
        return status


def delivery_provider_label(code: str) -> str:
    try:
        return DELIVERY_PROVIDER_LABELS[DeliveryProviderCode(code)]
    except ValueError:
        return code


def payment_provider_label(code: str) -> str:
    try:
        return PAYMENT_PROVIDER_LABELS[PaymentProvider(code)]
    except ValueError:
        return code


def order_event_label(event_type: str) -> str:
    try:
        return ORDER_EVENT_LABELS[OrderEventType(event_type)]
    except ValueError:
        return event_type


def cancel_reason_label(code: str | None) -> str:
    if not code:
        return ""
    try:
        return CANCEL_REASON_LABELS[CancelReason(code)]
    except ValueError:
        return code


def admin_role_label(code: str) -> str:
    try:
        return ADMIN_ROLE_LABELS[AdminRole(code)]
    except ValueError:
        return code


def receipt_status_label(code: str) -> str:
    try:
        return RECEIPT_STATUS_LABELS[ReceiptStatus(code)]
    except ValueError:
        return code


def receipt_provider_label(code: str) -> str:
    try:
        return RECEIPT_PROVIDER_LABELS[ReceiptProvider(code)]
    except ValueError:
        return code
