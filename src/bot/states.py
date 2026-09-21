"""Состояния оформления заказа.

Порядок состояний повторяет шаги сценария из ТЗ. Сам черновик заказа живёт в базе,
в FSM хранится только то, что нужно для текущего шага: id сообщения, которое
редактируем, и промежуточный выбор цветов.
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class Checkout(StatesGroup):
    """Восемь шагов оформления."""

    product = State()  # 1. какой бокс
    spoons = State()  # 2. сколько ложечек
    video = State()  # 3. видео сборки
    favorite_colors = State()  # 4. любимые цвета
    avoid_colors = State()  # 5. нежелательные цвета
    comment = State()  # 6. пожелания
    pickup = State()  # 7. пункт выдачи
    recipient_name = State()  # 8. имя
    recipient_phone = State()  # 8. телефон
    recipient_email = State()  # 8. email (по желанию)
    summary = State()  # итог и оплата


#: Ключи данных FSM.
UI_MESSAGE_ID = "ui_message_id"
PHOTO_MESSAGE_ID = "photo_message_id"
ORDER_ID = "order_id"
PRODUCT_ID = "product_id"
SPOON_COUNT = "spoon_count"
WITH_VIDEO = "with_video"
FAVORITE_COLORS = "favorite_colors"
AVOID_COLORS = "avoid_colors"
FAVORITE_CUSTOM = "favorite_custom"
AVOID_CUSTOM = "avoid_custom"
PICKUP_QUERY = "pickup_query"
PICKUP_OFFSET = "pickup_offset"
PICKUP_LATITUDE = "pickup_latitude"
PICKUP_LONGITUDE = "pickup_longitude"
#: Возврат к итогу после точечной правки одного шага.
EDITING = "editing"
