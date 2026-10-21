"""Фабрики callback_data.

Телеграм отводит на callback_data 64 байта, поэтому в кнопках ездят короткие
префиксы и числовые id, а не названия и тем более не цены.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class ProductCB(CallbackData, prefix="p"):
    """Выбор бокса."""

    product_id: int


class SpoonCB(CallbackData, prefix="sp"):
    """Сколько ложечек в боксе."""

    count: int


class VideoCB(CallbackData, prefix="vd"):
    """Нужно ли видео сборки."""

    enabled: bool


class PickupCB(CallbackData, prefix="pv"):
    """Выбранный пункт выдачи."""

    point_id: int


class NearestPointCB(CallbackData, prefix="np"):
    """Показать на карте пункт из списка ближайших (вне оформления заказа)."""

    point_id: int


class ManualPickupCB(CallbackData, prefix="mp"):
    """Пункт, которого нет в списке: клиент выбирает службу, город и пишет адрес сам.

    step: «provider» — выбрать службу, «area» — в Москве или нет, «address» — ввести адрес.
    """

    step: str
    provider: str = ""
    moscow: bool = False


class PickupPageCB(CallbackData, prefix="pvp"):
    """Листание списка пунктов выдачи."""

    offset: int


class StepCB(CallbackData, prefix="st"):
    """Переход по шагам: назад или правка раздела из итога."""

    step: str


class SkipCB(CallbackData, prefix="sk"):
    """Пропустить необязательный шаг."""

    step: str


class CartCB(CallbackData, prefix="ct"):
    """Действие в корзине: add — ещё бокс, edit/remove — с конкретным боксом, next — дальше."""

    action: str
    item_id: int = 0


class OrderCB(CallbackData, prefix="o"):
    """Действие над заказом в «Моих заказах»: open, pay, cancel."""

    order_id: int
    action: str


class ConfirmCB(CallbackData, prefix="cf"):
    """Подтверждение действия: да/нет."""

    action: str
    value: int


class PrefillCB(CallbackData, prefix="pf"):
    """Подставленные данные: use=True — оставить, False — ввести заново.

    what: draft — незаконченное оформление, pickup — пункт выдачи, recipient — получатель.
    """

    what: str
    use: bool


class OwnerPhotoCB(CallbackData, prefix="ph"):
    """Фото бокса в рабочем режиме: pick — выбрать бокс, clear — убрать, list — назад."""

    action: str
    product_id: int


class MenuCB(CallbackData, prefix="m"):
    """Раздел главного меню: home, order, orders, prices, delivery, faq, contacts."""

    section: str
