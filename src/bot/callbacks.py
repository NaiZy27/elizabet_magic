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


class ColorCB(CallbackData, prefix="c"):
    """Отметить цвет. kind: f — любимые, a — нежелательные."""

    kind: str
    color_id: int


class ColorsDoneCB(CallbackData, prefix="cd"):
    """Закончить выбор цветов."""

    kind: str


class PickupCB(CallbackData, prefix="pv"):
    """Выбранный пункт выдачи."""

    point_id: int


class PickupPageCB(CallbackData, prefix="pvp"):
    """Листание списка пунктов выдачи."""

    offset: int


class StepCB(CallbackData, prefix="st"):
    """Переход по шагам: назад или правка раздела из итога."""

    step: str


class SkipCB(CallbackData, prefix="sk"):
    """Пропустить необязательный шаг."""

    step: str


class OrderCB(CallbackData, prefix="o"):
    """Действие над заказом в «Моих заказах»: open, pay, cancel."""

    order_id: int
    action: str


class ConfirmCB(CallbackData, prefix="cf"):
    """Подтверждение действия: да/нет."""

    action: str
    value: int


class PrefillCB(CallbackData, prefix="pf"):
    """Использовать данные прошлого заказа."""

    use: bool
