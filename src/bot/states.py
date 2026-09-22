"""Состояния оформления заказа.

Черновик заказа живёт в базе, в FSM хранится только то, что нужно прямо сейчас:
id сообщений, которые редактируем, и бокс, который клиент собирает в эту минуту
(он попадает в заказ целиком, когда пройден последний шаг бокса).
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class Checkout(StatesGroup):
    """Шаги оформления: сначала боксы по одному, потом общие доставка и получатель."""

    # Бокс — эти шаги проходятся для каждого бокса в заказе.
    product = State()
    spoons = State()
    video = State()
    favorite_colors = State()
    avoid_colors = State()
    comment = State()
    # Корзина: добавить ещё бокс, поправить или убрать.
    cart = State()
    # Общее для всего заказа.
    pickup = State()
    pickup_confirm = State()
    recipient_confirm = State()
    recipient_name = State()
    recipient_phone = State()
    recipient_email = State()
    summary = State()


#: Шагов у одного бокса — показывается как «Бокс 2 · шаг 3 из 6».
BOX_STEPS = 6

#: Ключи данных FSM.
UI_MESSAGE_ID = "ui_message_id"
#: Текст, показанный в сообщении сценария, — чтобы дописать под ним итог выбора.
UI_TEXT = "ui_text"
PHOTO_MESSAGE_ID = "photo_message_id"
VENUE_MESSAGE_ID = "venue_message_id"
HELPER_MESSAGE_ID = "helper_message_id"
ORDER_ID = "order_id"

# Бокс, который собирается сейчас.
EDIT_ITEM_ID = "edit_item_id"
BOX_NUMBER = "box_number"
PRODUCT_ID = "product_id"
SPOON_COUNT = "spoon_count"
WITH_VIDEO = "with_video"
FAVORITE_COLORS = "favorite_colors"
AVOID_COLORS = "avoid_colors"
COMMENT = "comment"

PICKUP_QUERY = "pickup_query"
PICKUP_OFFSET = "pickup_offset"
PICKUP_LATITUDE = "pickup_latitude"
PICKUP_LONGITUDE = "pickup_longitude"
RECIPIENT_NAME = "recipient_name"
RECIPIENT_PHONE = "recipient_phone"
#: Возврат к итогу после точечной правки одного раздела.
EDITING = "editing"

#: Всё, что относится к собираемому боксу, — очищается перед следующим.
BOX_KEYS = (
    EDIT_ITEM_ID,
    BOX_NUMBER,
    PRODUCT_ID,
    SPOON_COUNT,
    WITH_VIDEO,
    FAVORITE_COLORS,
    AVOID_COLORS,
    COMMENT,
)
