"""Оформление заказа в одном сообщении.

Порядок: боксы по одному (у каждого свои ложечки, видео и пожелания текстом) →
корзина → пункт выдачи → получатель → итог → оплата. Постоянному клиенту пункт
выдачи и получатель подставляются из прошлого заказа — остаётся подтвердить.

Черновик живёт в базе, поэтому клиент может уйти и вернуться через день. Любая
кнопка сначала проверяет, что черновик из FSM всё ещё актуален: если заказ уже
оформлен или удалён, кнопки на сообщении снимаются, и клиент начинает заново.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import ui
from bot.callbacks import (
    CartCB,
    ConfirmCB,
    MenuCB,
    PickupCB,
    PickupPageCB,
    PrefillCB,
    ProductCB,
    SkipCB,
    SpoonCB,
    StepCB,
    VideoCB,
)
from bot.emoji import provider_icon
from bot.keyboards import checkout as kb
from bot.keyboards.common import (
    BTN_CANCEL,
    BTN_ORDER,
    BTN_SEND_PHONE,
    MENU_BUTTONS,
    main_menu,
    request_location,
    request_phone,
    single_button,
)
from bot.states import (
    AVOID_COLORS,
    BOX_KEYS,
    BOX_NUMBER,
    COMMENT,
    EDIT_ITEM_ID,
    EDITING,
    FAVORITE_COLORS,
    HELPER_MESSAGE_ID,
    ORDER_ID,
    PICKUP_LATITUDE,
    PICKUP_LONGITUDE,
    PICKUP_OFFSET,
    PICKUP_QUERY,
    PRODUCT_ID,
    RECIPIENT_NAME,
    RECIPIENT_PHONE,
    SPOON_COUNT,
    WITH_VIDEO,
    Checkout,
)
from core.clock import format_date
from core.config import get_settings
from core.enums import MessageTemplateKey
from core.errors import DomainError
from core.models import Customer, Order, OrderItem
from core.money import format_rubles
from core.services import (
    board,
    catalog,
    delivery,
    intake,
    notifications,
    orders,
    payments,
    settings,
)
from core.services.catalog import active_variants, price_for_spoons, spoon_options
from core.text import truncate

#: Текстовые шаги не должны принимать нажатия кнопок меню за ответ клиента.
NOT_MENU = ~F.text.in_(MENU_BUTTONS)

logger = logging.getLogger(__name__)

router = Router(name="checkout")

STALE_TEXT = "Это оформление устарело — начните заново."


# --- вход в сценарий ---


@router.callback_query(MenuCB.filter(F.section == "order"))
async def start_from_menu(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    customer: Customer,
) -> None:
    await callback.answer()
    await start_checkout(callback, state, session, customer)


@router.message(F.text == BTN_ORDER)
async def start_checkout(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    customer: Customer,
) -> None:
    """Начать оформление или предложить продолжить брошенный черновик.

    Из меню оформление продолжается в том же сообщении, где было меню.
    """
    opened = await intake.check(session)
    if not opened.is_open:
        await _reset(event, state)
        await ui.reply(event, text=opened.message, keyboard=main_menu())
        return

    texts = await settings.get_bot_texts(session)
    if customer.consent_accepted_at is None:
        await _reset(event, state)
        policy = (
            f'\n\n<a href="{texts.consent_url}">Политика обработки данных</a>'
            if texts.consent_url
            else ""
        )
        await ui.reply(
            event,
            text=f"{texts.consent_text}{policy}",
            keyboard=single_button("Продолжить", ConfirmCB(action="consent", value=1).pack()),
        )
        return

    draft = await orders.get_draft(session, customer.id)
    if draft is not None and draft.items:
        await _reset(event, state)
        await ui.adopt(event, state)
        await state.update_data({ORDER_ID: draft.id})
        await ui.show(
            event,
            state,
            text=ui.compose(
                None,
                "Есть незаконченный заказ",
                notifications.describe_items(draft),
            ),
            keyboard=kb.resume_draft(),
        )
        return

    await _begin_new_order(event, state, session, customer)


@router.callback_query(ConfirmCB.filter(F.action == "consent"))
async def accept_consent(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    customer: Customer,
) -> None:
    await orders.accept_consent(session, customer)
    await callback.answer()
    await _begin_new_order(callback, state, session, customer)


@router.callback_query(PrefillCB.filter(F.what == "draft"))
async def continue_or_restart(
    callback: CallbackQuery,
    callback_data: PrefillCB,
    state: FSMContext,
    session: AsyncSession,
    customer: Customer,
) -> None:
    """«Продолжим с того же места?» — да или начать заново."""
    if not callback_data.use:
        await callback.answer()
        await _begin_new_order(callback, state, session, customer)
        return

    order = await _draft(callback, state, session, customer)
    if order is None:
        return
    await callback.answer()
    # Цены могли поменяться, пока черновик лежал.
    await orders.recalculate(session, order)
    if order.items:
        await show_cart(callback, state, session, order)
    else:
        await _start_box(callback, state, session, order)


async def _begin_new_order(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    customer: Customer,
    *,
    footer: str | None = None,
) -> None:
    order = await orders.create_draft(session, customer)
    await _reset(event, state, footer=footer)
    # Нажали кнопку в сообщении — дальше оформление идёт прямо в нём.
    await ui.adopt(event, state)
    await state.update_data({ORDER_ID: order.id})
    await _start_box(event, state, session, order)


# --- бокс: шаг 1, какой ---


async def _start_box(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    order: Order,
    item: OrderItem | None = None,
) -> None:
    """Начать собирать новый бокс или открыть на правку уже добавленный."""
    await state.update_data(dict.fromkeys(BOX_KEYS))
    if item is None:
        await state.update_data({BOX_NUMBER: len(order.items) + 1})
    else:
        await state.update_data(
            {
                EDIT_ITEM_ID: item.id,
                BOX_NUMBER: order.items.index(item) + 1,
                PRODUCT_ID: item.variant.product_id,
                SPOON_COUNT: item.spoon_count,
                WITH_VIDEO: item.has_video,
                FAVORITE_COLORS: await catalog.color_ids_by_names(session, item.favorite_colors),
                AVOID_COLORS: await catalog.color_ids_by_names(session, item.avoid_colors),
                COMMENT: item.comment,
            },
        )
    await show_product_step(event, state, session, has_items=bool(order.items))


async def show_product_step(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    *,
    has_items: bool,
) -> None:
    products = await catalog.list_active_products(session)
    if not products:
        await ui.show(
            event,
            state,
            text="Каталог обновляется — загляните чуть позже.",
        )
        return

    rows = [
        (product, min(variant.price_kopecks for variant in active_variants(product.variants)))
        for product in products
    ]
    data = await state.get_data()
    await state.set_state(Checkout.product)
    await ui.clear_photo(event, state)
    await ui.show(
        event,
        state,
        text=ui.compose(
            ui.box_header(data.get(BOX_NUMBER) or 1, 1),
            "Выберите бокс",
        ),
        keyboard=kb.products(rows, back_to_cart=has_items),
    )


@router.callback_query(ProductCB.filter())
async def choose_product(
    callback: CallbackQuery,
    callback_data: ProductCB,
    state: FSMContext,
    session: AsyncSession,
    customer: Customer,
) -> None:
    if await _draft(callback, state, session, customer) is None:
        return
    await callback.answer()
    data = await state.get_data()
    if data.get(PRODUCT_ID) != callback_data.product_id:
        # Другой бокс — прежнее число ложечек к нему может не подойти.
        await state.update_data({SPOON_COUNT: None})
    await state.update_data({PRODUCT_ID: callback_data.product_id})
    await show_spoons_step(callback, state, session)


# --- бокс: шаг 2, ложечки ---


async def show_spoons_step(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    data = await state.get_data()
    product = await catalog.get_product(session, data[PRODUCT_ID])
    if not product.is_active or not active_variants(product.variants):
        await show_product_step(event, state, session, has_items=True)
        return

    options: list[tuple[int, int]] = []
    for count in spoon_options(product, product.variants):
        _, price = price_for_spoons(product, product.variants, count)
        options.append((count, price))

    body = product.description or ""
    if product.allows_extra_spoons and product.extra_spoon_price_kopecks is not None:
        extra = format_rubles(product.extra_spoon_price_kopecks)
        body = f"{body}\n\n+1 ложечка — {extra}".strip()

    await state.set_state(Checkout.spoons)
    if product.photo_file_id:
        await ui.show_photo(event, state, file_id=product.photo_file_id, caption=product.name)
    await ui.show(
        event,
        state,
        text=ui.compose(
            ui.box_header(data.get(BOX_NUMBER) or 1, 2),
            "Сколько ложечек?",
            body,
        ),
        keyboard=kb.spoons(options, chosen=data.get(SPOON_COUNT)),
    )


@router.callback_query(SpoonCB.filter())
async def choose_spoons(
    callback: CallbackQuery,
    callback_data: SpoonCB,
    state: FSMContext,
    session: AsyncSession,
    customer: Customer,
) -> None:
    if await _draft(callback, state, session, customer) is None:
        return
    await callback.answer()
    await state.update_data({SPOON_COUNT: callback_data.count})
    await show_video_step(callback, state, session)


# --- бокс: шаг 3, видео сборки ---


async def show_video_step(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    addon = await catalog.get_video_addon(session)
    if addon is None:
        # Услуга выключена в админке — вопрос не задаём.
        await state.update_data({WITH_VIDEO: False})
        await show_comment_step(event, state, session)
        return

    data = await state.get_data()
    texts = await settings.get_bot_texts(session)
    await state.set_state(Checkout.video)
    await ui.clear_photo(event, state)
    description = addon.description or "Снимем сборку именно вашего бокса и пришлём видео."
    note = texts.no_video_note.strip()
    await ui.show(
        event,
        state,
        text=ui.compose(
            ui.box_header(data.get(BOX_NUMBER) or 1, 3),
            "Видео сборки?",
            # Подпись про бокс без видео владелица правит в панели: «Настройки → Тексты».
            f"{description}\n\n<i>{note}</i>" if note else description,
        ),
        keyboard=kb.video(addon.price_kopecks, chosen=data.get(WITH_VIDEO)),
    )


@router.callback_query(VideoCB.filter())
async def choose_video(
    callback: CallbackQuery,
    callback_data: VideoCB,
    state: FSMContext,
    session: AsyncSession,
    customer: Customer,
) -> None:
    if await _draft(callback, state, session, customer) is None:
        return
    await callback.answer()
    await state.update_data({WITH_VIDEO: callback_data.enabled})
    await show_comment_step(callback, state, session)


# --- бокс: шаг 4, цвета и пожелания текстом ---


async def show_comment_step(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    texts = await settings.get_bot_texts(session)
    data = await state.get_data()
    current = data.get(COMMENT)
    hint = f"\n\nСейчас: «{truncate(current, 300)}»" if current else ""
    back_step = "video" if await catalog.get_video_addon(session) is not None else "spoons"
    await state.set_state(Checkout.comment)
    await ui.clear_photo(event, state)
    await ui.show(
        event,
        state,
        text=ui.compose(
            ui.box_header(data.get(BOX_NUMBER) or 1, 4),
            "Цвета и пожелания",
            "Напишите одним сообщением: какие цвета нравятся, какие не использовать, "
            f"для кого подарок.{hint}\n\n<i>{texts.wishes_disclaimer}</i>",
        ),
        keyboard=kb.comment(has_current=bool(current), back_step=back_step),
    )


@router.message(Checkout.comment, F.text, NOT_MENU)
async def receive_comment(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    customer: Customer,
) -> None:
    order = await _draft(message, state, session, customer)
    if order is None:
        return
    comment = (message.text or "").strip() or None
    await state.update_data({COMMENT: comment})
    await ui.consume(message, state)
    await _finish_box(message, state, session, order)


@router.callback_query(SkipCB.filter(F.step == "comment"))
async def skip_comment(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    customer: Customer,
) -> None:
    """Без пожеланий — или, при правке бокса, оставить прежние."""
    order = await _draft(callback, state, session, customer)
    if order is None:
        return
    await callback.answer()
    await _finish_box(callback, state, session, order)


async def _finish_box(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    order: Order,
) -> None:
    """Все шаги бокса пройдены — кладём его в заказ и показываем корзину."""
    data = await state.get_data()
    if data.get(PRODUCT_ID) is None or data.get(SPOON_COUNT) is None:
        await show_product_step(event, state, session, has_items=bool(order.items))
        return

    try:
        product = await catalog.get_product(session, data[PRODUCT_ID])
        variant, _ = price_for_spoons(product, product.variants, data[SPOON_COUNT])
        video = await catalog.get_video_addon(session)
        await orders.save_box(
            session,
            order,
            orders.BoxRequest(
                variant_id=variant.id,
                spoon_count=data[SPOON_COUNT],
                addon_ids=(video.id,) if (data.get(WITH_VIDEO) and video is not None) else (),
                favorite_color_ids=tuple(data.get(FAVORITE_COLORS) or ()),
                avoid_color_ids=tuple(data.get(AVOID_COLORS) or ()),
                comment=data.get(COMMENT),
            ),
            item_id=data.get(EDIT_ITEM_ID),
        )
    except DomainError as error:
        await ui.show(
            event,
            state,
            text=ui.compose(None, "Не получилось добавить бокс", error.message),
            keyboard=kb.cart(order.items, can_add=True) if order.items else None,
        )
        return

    await state.update_data(dict.fromkeys(BOX_KEYS))
    await show_cart(event, state, session, order)


# --- корзина ---


async def show_cart(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    order: Order,
) -> None:
    rules = await settings.get_order_rules(session)
    await state.set_state(Checkout.cart)
    await ui.clear_photo(event, state)
    body = (
        f"{describe_boxes(order)}\n\nБоксы и услуги: <b>{format_rubles(order.subtotal_kopecks)}</b>"
    )
    await ui.show(
        event,
        state,
        text=ui.compose(None, "Ваш заказ", body),
        keyboard=kb.cart(order.items, can_add=order.box_count < rules.max_boxes_per_order),
    )


def describe_boxes(order: Order) -> str:
    """Боксы построчно: цена бокса, цена каждой услуги и сумма — как в чеке.

    «Бокс 3 ложечки — 1 200 ₽ / + видео сборки — 300 ₽ / = 1 500 ₽».
    """
    blocks = []
    many = len(order.items) > 1
    for index, item in enumerate(order.items, start=1):
        prefix = f"{index}. " if many else ""
        name = (
            item.name_snapshot if item.quantity == 1 else f"{item.name_snapshot} × {item.quantity}"
        )
        lines = [
            f"{prefix}<b>{name}</b> — {format_rubles(item.unit_price_kopecks * item.quantity)}",
        ]
        for addon in item.addons:
            lines.append(
                f"   + {addon.name_snapshot.lower()} — {format_rubles(addon.total_kopecks)}"
            )
        if item.addons:
            lines.append(f"   = {format_rubles(item.total_kopecks)}")
        wishes = ", ".join(
            part
            for part in (
                f"цвета: {', '.join(item.favorite_colors)}" if item.favorite_colors else "",
                f"без: {', '.join(item.avoid_colors)}" if item.avoid_colors else "",
                item.comment or "",
            )
            if part
        )
        if wishes:
            lines.append(f"   ✍️ {truncate(wishes, 120)}")
        blocks.append("\n".join(lines))
    blocks.extend(
        f"{addon.name_snapshot} — {format_rubles(addon.total_kopecks)}"
        for addon in order.order_addons
    )
    return "\n".join(blocks)


@router.callback_query(CartCB.filter())
async def cart_action(
    callback: CallbackQuery,
    callback_data: CartCB,
    state: FSMContext,
    session: AsyncSession,
    customer: Customer,
) -> None:
    order = await _draft(callback, state, session, customer)
    if order is None:
        return

    action = callback_data.action
    if action == "add":
        rules = await settings.get_order_rules(session)
        if order.box_count >= rules.max_boxes_per_order:
            await callback.answer(
                f"В одном заказе — не больше {rules.max_boxes_per_order} боксов",
                show_alert=True,
            )
            return
        await callback.answer()
        await _start_box(callback, state, session, order)
        return

    item = next((item for item in order.items if item.id == callback_data.item_id), None)
    if action in {"edit", "remove"} and item is None:
        await callback.answer("Этого бокса уже нет в заказе")
        await show_cart(callback, state, session, order)
        return

    if action == "edit":
        await callback.answer()
        await _start_box(callback, state, session, order, item)
    elif action == "remove":
        await orders.remove_box(session, order, item.id)
        await callback.answer("Бокс убран")
        if order.items:
            await show_cart(callback, state, session, order)
        else:
            await _start_box(callback, state, session, order)
    elif action == "next":
        await callback.answer()
        if not order.items:
            await _start_box(callback, state, session, order)
        elif (await state.get_data()).get(EDITING):
            await show_summary(callback, state, session, order)
        else:
            await _delivery_step(callback, state, session, order)


# --- пункт выдачи ---


async def _delivery_step(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    order: Order,
) -> None:
    """Пункт из прошлого заказа уже подставлен — предлагаем его, иначе ищем."""
    if order.pickup_point_id is not None and order.pickup_snapshot:
        await show_pickup_confirm(event, state, order)
    else:
        await show_pickup_search_step(event, state, session)


async def show_pickup_confirm(
    event: Message | CallbackQuery,
    state: FSMContext,
    order: Order,
) -> None:
    snapshot = order.pickup_snapshot or {}
    lines = [f"{provider_icon(snapshot.get('provider'))} <b>{snapshot.get('address', '')}</b>"]
    if snapshot.get("working_hours"):
        lines.append(snapshot["working_hours"])
    lines.extend(["", f"Доставка: {format_rubles(order.delivery_kopecks)}, входит в чек"])
    await state.set_state(Checkout.pickup_confirm)
    await ui.show(
        event,
        state,
        text=ui.compose(None, "Туда же, куда в прошлый раз?", "\n".join(lines)),
        keyboard=kb.confirm_prefilled("pickup", keep="✅ Да, сюда", change="📍 Другой пункт"),
    )


@router.callback_query(PrefillCB.filter(F.what == "pickup"))
async def confirm_pickup(
    callback: CallbackQuery,
    callback_data: PrefillCB,
    state: FSMContext,
    session: AsyncSession,
    customer: Customer,
) -> None:
    order = await _draft(callback, state, session, customer)
    if order is None:
        return
    await callback.answer()
    if callback_data.use and order.pickup_point_id is not None:
        await _after_pickup(callback, state, session, order)
    else:
        await show_pickup_search_step(callback, state, session)


async def show_pickup_search_step(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await state.set_state(Checkout.pickup)
    await state.update_data({PICKUP_OFFSET: 0, PICKUP_QUERY: None})
    await ui.show(
        event,
        state,
        text=ui.compose(
            None,
            "Куда доставить?",
            "📍 Отправьте геопозицию или напишите город и улицу.\n\n"
            f"{await _delivery_terms(session)}",
        ),
        keyboard=kb.pickup_search(),
    )
    # Геопозицию Telegram отдаёт только кнопкой под полем ввода — инлайн её не запросить.
    await ui.show_helper(event, state, text="👇", keyboard=request_location())


async def _delivery_terms(session: AsyncSession) -> str:
    """«📦 СДЭК · 🛍 Яндекс Доставка — 500 ₽, фиксированная, входит в чек»."""
    providers = await delivery.enabled_providers(session)
    names = " · ".join(f"{provider_icon(provider.code)} {provider.name}" for provider in providers)
    fixed = await delivery.fixed_price_kopecks(session)
    if fixed is None:
        return names
    price = f"Доставка — {format_rubles(fixed)}, фиксированная, входит в чек."
    return f"{names}\n{price}" if names else price


@router.message(Checkout.pickup, F.location)
async def receive_location(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    customer: Customer,
) -> None:
    """Ближайшие пункты по геопозиции. Саму точку клиента мы не храним."""
    if await _draft(message, state, session, customer) is None:
        return
    location = message.location
    await state.update_data(
        {
            PICKUP_LATITUDE: location.latitude,
            PICKUP_LONGITUDE: location.longitude,
            PICKUP_QUERY: None,
            PICKUP_OFFSET: 0,
        },
    )
    await _leave_reply_step(message, state)
    await ui.consume(message, state)
    await _show_pickup_points(message, state, session)


@router.message(Checkout.pickup, F.text, NOT_MENU)
async def receive_city(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    customer: Customer,
) -> None:
    if await _draft(message, state, session, customer) is None:
        return
    query = (message.text or "").strip()
    if len(query) < 2:
        await message.answer("Напишите город или улицу целиком.")
        return

    await state.update_data(
        {PICKUP_QUERY: query, PICKUP_LATITUDE: None, PICKUP_LONGITUDE: None, PICKUP_OFFSET: 0},
    )
    await _leave_reply_step(message, state)
    await ui.consume(message, state)
    await _show_pickup_points(message, state, session)


@router.callback_query(PickupPageCB.filter())
async def paginate_points(
    callback: CallbackQuery,
    callback_data: PickupPageCB,
    state: FSMContext,
    session: AsyncSession,
    customer: Customer,
) -> None:
    if await _draft(callback, state, session, customer) is None:
        return
    await callback.answer()
    await state.update_data({PICKUP_OFFSET: callback_data.offset})
    await _show_pickup_points(callback, state, session)


async def _show_pickup_points(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    data = await state.get_data()
    environment = get_settings().delivery_environment
    offset = data.get(PICKUP_OFFSET, 0)
    page = delivery.PICKUP_POINTS_PAGE_SIZE

    latitude = data.get(PICKUP_LATITUDE)
    longitude = data.get(PICKUP_LONGITUDE)
    query = data.get(PICKUP_QUERY)

    distances: dict[int, float] = {}
    note = ""
    by_location = latitude is not None and longitude is not None
    if by_location:
        # По геопозиции показываем ближайшие одной страницей: листать «дальше от дома»
        # смысла нет, проще уточнить адрес словами.
        found = await delivery.search_nearest(
            session,
            latitude=latitude,
            longitude=longitude,
            environment=environment,
            limit=page,
        )
        points = [point for point, _ in found]
        distances = {point.id: km for point, km in found}
    elif query:
        found_by_text = await delivery.search_by_text(
            session,
            query,
            environment=environment,
            limit=page + 1,
            offset=offset,
        )
        points = found_by_text.points
        if not found_by_text.exact and points:
            note = f"Точно по адресу не нашли — пункты по запросу «{found_by_text.fallback_word}»."
    else:
        await show_pickup_search_step(event, state, session)
        return

    has_more = not by_location and len(points) > page
    points = points[:page]

    if not points:
        await ui.show(
            event,
            state,
            text=ui.compose(
                None,
                "Ничего не нашли",
                "Напишите только город — например, «Казань» — или отправьте геопозицию.",
            ),
            keyboard=kb.pickup_search(),
        )
        return

    lines = [note, ""] if note else []
    for point in points:
        distance = distances.get(point.id)
        suffix = f" · {distance:.1f} км".replace(".", ",") if distance is not None else ""
        lines.append(
            f"{provider_icon(point.provider)} <b>{truncate(point.address, 80)}</b>{suffix}"
        )
        if point.working_hours:
            lines.append(f"      {truncate(point.working_hours, 80)}")
    lines.extend(["", await _delivery_terms(session)])

    await ui.show(
        event,
        state,
        text=ui.compose(None, "Выберите пункт выдачи", "\n".join(lines)),
        keyboard=kb.pickup_points(points, offset=offset, has_more=has_more),
    )


@router.callback_query(PickupCB.filter())
async def choose_pickup_point(
    callback: CallbackQuery,
    callback_data: PickupCB,
    state: FSMContext,
    session: AsyncSession,
    customer: Customer,
) -> None:
    """Сохранить выбранный пункт, показать его на карте и посчитать доставку."""
    order = await _draft(callback, state, session, customer)
    if order is None:
        return
    try:
        point = await delivery.get_pickup_point(session, callback_data.point_id)
        await orders.set_pickup_point(session, order, point)
    except DomainError as error:
        await callback.answer(error.message, show_alert=True)
        return
    await callback.answer()

    await _leave_reply_step(callback, state)
    # Отдельную карту не шлём: оформление идёт в одном сообщении, адрес виден в итоге.
    await _after_pickup(callback, state, session, order)


async def _after_pickup(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    order: Order,
) -> None:
    if (await state.get_data()).get(EDITING):
        await show_summary(event, state, session, order)
    else:
        await _recipient_step(event, state, session, order)


# --- получатель ---


async def _recipient_step(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    order: Order,
) -> None:
    """Контакты из прошлого заказа — подтвердить одной кнопкой, иначе спросить."""
    if (
        order.recipient_name
        and order.recipient_phone
        and orders.is_valid_email(order.recipient_email)
    ):
        await state.set_state(Checkout.recipient_confirm)
        await ui.show(
            event,
            state,
            text=ui.compose(
                None,
                "Получатель — всё верно?",
                f"👤 {order.recipient_name}\n📱 {order.recipient_phone}\n✉️ {order.recipient_email}",
            ),
            keyboard=kb.confirm_prefilled("recipient", keep="✅ Всё верно", change="✏️ Изменить"),
        )
        return
    await show_recipient_name_step(event, state, order)


@router.callback_query(PrefillCB.filter(F.what == "recipient"))
async def confirm_recipient(
    callback: CallbackQuery,
    callback_data: PrefillCB,
    state: FSMContext,
    session: AsyncSession,
    customer: Customer,
) -> None:
    order = await _draft(callback, state, session, customer)
    if order is None:
        return
    await callback.answer()
    if callback_data.use:
        await show_summary(callback, state, session, order)
    else:
        await show_recipient_name_step(callback, state, order)


async def show_recipient_name_step(
    event: Message | CallbackQuery,
    state: FSMContext,
    order: Order,
) -> None:
    await state.set_state(Checkout.recipient_name)
    current = order.recipient_name or ""
    hint = f"Сейчас: <b>{current}</b>" if current else ""
    await ui.show(event, state, text=ui.compose(None, "Имя получателя", hint))


@router.message(Checkout.recipient_name, F.text, NOT_MENU)
async def receive_name(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    customer: Customer,
) -> None:
    if await _draft(message, state, session, customer) is None:
        return
    name = (message.text or "").strip()
    if len(name) < 2:
        await message.answer("Напишите имя полностью.")
        return

    await state.update_data({RECIPIENT_NAME: name})
    await state.set_state(Checkout.recipient_phone)
    await ui.consume(message, state)
    await ui.show(
        message,
        state,
        text=ui.compose(None, "Телефон получателя", f"Нажмите «{BTN_SEND_PHONE}» внизу."),
    )
    # Контакт Telegram отдаёт только кнопкой под полем ввода — инлайн её не запросить.
    await ui.show_helper(message, state, text="👇", keyboard=request_phone())


@router.message(Checkout.recipient_phone, F.contact)
async def receive_contact(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    customer: Customer,
) -> None:
    await _save_phone(message, state, session, customer, phone=message.contact.phone_number)


@router.message(Checkout.recipient_phone, F.text, NOT_MENU)
async def receive_phone_text(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    customer: Customer,
) -> None:
    phone = (message.text or "").strip()
    digits = "".join(character for character in phone if character.isdigit())
    if len(digits) < 10:
        await message.answer("Номер неполный. Пример: +7 900 123-45-67")
        return
    await _save_phone(message, state, session, customer, phone=phone)


async def _save_phone(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    customer: Customer,
    *,
    phone: str,
) -> None:
    order = await _draft(message, state, session, customer)
    if order is None:
        return
    await state.update_data({RECIPIENT_PHONE: phone})
    await _leave_reply_step(message, state)
    await ui.consume(message, state)
    await show_email_step(message, state, order)


async def show_email_step(
    event: Message | CallbackQuery,
    state: FSMContext,
    order: Order,
) -> None:
    current = order.recipient_email if orders.is_valid_email(order.recipient_email) else None
    hint = f"Сейчас: <b>{current}</b>" if current else ""
    await state.set_state(Checkout.recipient_email)
    await ui.show(
        event,
        state,
        text=ui.compose(None, "Email для чека", hint),
        keyboard=kb.email_step(has_current=current is not None),
    )


@router.message(Checkout.recipient_email, F.text, NOT_MENU)
async def receive_email(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    customer: Customer,
) -> None:
    email = (message.text or "").strip()
    if not orders.is_valid_email(email):
        await message.answer("Похоже на опечатку. Пример: anna@mail.ru")
        return
    await ui.consume(message, state)
    await _save_recipient(message, state, session, customer, email=email)


@router.callback_query(SkipCB.filter(F.step == "email"))
async def keep_email(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    customer: Customer,
) -> None:
    order = await _draft(callback, state, session, customer)
    if order is None:
        return
    await callback.answer()
    await _save_recipient(callback, state, session, customer, email=order.recipient_email or "")


async def _save_recipient(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    customer: Customer,
    *,
    email: str,
) -> None:
    order = await _draft(event, state, session, customer)
    if order is None:
        return
    data = await state.get_data()
    try:
        await orders.set_recipient(
            session,
            order,
            name=data.get(RECIPIENT_NAME) or order.recipient_name or customer.display_name,
            phone=data.get(RECIPIENT_PHONE) or order.recipient_phone or "",
            email=email,
            customer=customer,
        )
    except DomainError as error:
        await ui.show(event, state, text=ui.compose(None, "Проверьте данные", error.message))
        await show_recipient_name_step(event, state, order)
        return
    await show_summary(event, state, session, order)


# --- итог ---


async def show_summary(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    order: Order,
) -> None:
    # Если по дороге что-то потерялось (пункт выдачи закрыли), возвращаем на нужный шаг.
    if not order.items:
        await _start_box(event, state, session, order)
        return
    if order.pickup_point_id is None:
        await state.update_data({EDITING: True})
        await show_pickup_search_step(event, state, session)
        return
    if not (
        order.recipient_name
        and order.recipient_phone
        and orders.is_valid_email(order.recipient_email)
    ):
        await state.update_data({EDITING: True})
        await show_recipient_name_step(event, state, order)
        return

    await state.set_state(Checkout.summary)
    await state.update_data({EDITING: False})
    await ui.clear_photo(event, state)
    calendar = await settings.get_working_calendar(session)
    spot = await board.estimate_spot(session, order, calendar=calendar)
    await ui.show(event, state, text=summary_text(order, spot), keyboard=kb.summary())


def summary_text(order: Order, spot: board.QueueSpot | None = None) -> str:
    snapshot = order.pickup_snapshot or {}
    lines = [
        describe_boxes(order),
        f"Доставка — {format_rubles(order.delivery_kopecks)}",
        "",
        f"<b>Итого: {format_rubles(order.total_kopecks)}</b>",
        "",
        f"{provider_icon(snapshot.get('provider'))} {snapshot.get('address', '')}",
        f"👤 {order.recipient_name}, {order.recipient_phone}",
        f"✉️ {order.recipient_email}",
    ]
    if spot is not None:
        lines.extend(
            [
                "",
                f"🗓 Место в очереди: {spot.position} · соберём к {format_date(spot.ready_date)}",
            ],
        )
    return ui.compose(None, "Проверьте заказ", "\n".join(lines))


@router.callback_query(StepCB.filter(F.step == "pay"))
async def pay(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    customer: Customer,
) -> None:
    """Оформить заказ и выдать ссылку на оплату."""
    order = await _draft(callback, state, session, customer)
    if order is None:
        return

    try:
        await orders.submit(session, order)
        payment = await payments.create_payment(session, order)
    except DomainError as error:
        await callback.answer(error.message, show_alert=True)
        return

    await callback.answer()
    url = payments.payment_url(payment)
    text = await notifications.render_for_order(
        session,
        order,
        MessageTemplateKey.ORDER_CREATED,
        pay_url=url,
    )
    await ui.clear_helper(callback, state)
    text = text or f"Заказ {order.display_number} оформлен, ждём оплату."
    if not payments.is_button_url(url):
        # Адрес виден только на этой машине — кнопку с ним Telegram не примет,
        # поэтому показываем ссылку текстом (бывает при локальной разработке).
        text = f"{text}\n\nСсылка на оплату:\n<code>{url}</code>"
        url = ""
    message_id = await ui.show(callback, state, text=text, keyboard=kb.payment(url, order.id))
    bot, chat_id = ui.target(callback)
    if message_id is not None and chat_id is not None:
        # Кнопки «Оплатить» и «Отменить» снимутся сами, когда заказ оплатят или отменят,
        # а под текстом появится итог — поэтому запоминаем и текст.
        orders.remember_bot_message(order, chat_id=chat_id, message_id=message_id, text=text)
    await state.clear()
    if bot is not None and chat_id is not None:
        # Оформление закончено — возвращаем главное меню, спрятанное на время заказа.
        await bot.send_message(
            chat_id=chat_id,
            text="После оплаты пришлём номер заказа и чек 💗",
            reply_markup=main_menu(),
        )


# --- навигация и отмена ---


@router.callback_query(StepCB.filter())
async def navigate(
    callback: CallbackQuery,
    callback_data: StepCB,
    state: FSMContext,
    session: AsyncSession,
    customer: Customer,
) -> None:
    """Кнопки «← Назад» и «✏️ Изменить …» из итога."""
    step = callback_data.step
    if step == "cancel":
        await callback.answer()
        await _cancel_checkout(callback, state, session, customer)
        return

    order = await _draft(callback, state, session, customer)
    if order is None:
        return
    await callback.answer()
    if step != "pickup_city":
        # Ушли с шага доставки кнопкой «Назад» — кнопка геопозиции больше не нужна.
        await _leave_reply_step(callback, state)

    # Правка из итога возвращает обратно в итог, а не гонит по всем шагам заново.
    if await state.get_state() == Checkout.summary.state:
        await state.update_data({EDITING: True})

    data = await state.get_data()
    if step == "product":
        await show_product_step(callback, state, session, has_items=bool(order.items))
    elif step == "spoons" and data.get(PRODUCT_ID):
        await show_spoons_step(callback, state, session)
    elif step == "video" and data.get(PRODUCT_ID):
        await show_video_step(callback, state, session)
    elif step in {"comment", "colors_f", "colors_a"}:
        # colors_* — кнопки «Назад» со старых сообщений, где ещё были цвета.
        await show_comment_step(callback, state, session)
    elif step == "cart":
        await state.update_data(dict.fromkeys(BOX_KEYS))
        if order.items:
            await show_cart(callback, state, session, order)
        else:
            await _start_box(callback, state, session, order)
    elif step == "pickup_search":
        await show_pickup_search_step(callback, state, session)
    elif step == "recipient":
        await state.update_data({RECIPIENT_NAME: None, RECIPIENT_PHONE: None})
        await show_recipient_name_step(callback, state, order)
    else:
        await show_product_step(callback, state, session, has_items=bool(order.items))


@router.message(F.text == BTN_CANCEL)
async def cancel_by_button(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    customer: Customer,
) -> None:
    await _cancel_checkout(message, state, session, customer)


async def _cancel_checkout(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    customer: Customer,
) -> None:
    draft = await orders.get_draft(session, customer.id)
    if draft is not None:
        await session.delete(draft)
        await session.flush()

    # Сообщение сценария становится меню — без лишних сообщений в чате.
    data = await state.get_data()
    await ui.clear_photo(event, state)
    await ui.clear_helper(event, state)
    await _leave_reply_keyboard(event, data)
    await state.clear()
    await ui.reply(event, text="Оформление отменено 💗", keyboard=main_menu())


# --- служебное ---


async def _reset(
    event: Message | CallbackQuery,
    state: FSMContext,
    *,
    footer: str | None = None,
) -> None:
    """Закрыть прошлое оформление: снять кнопки, убрать фото и подсказки, очистить FSM."""
    await ui.retire(event, state, footer=footer)
    await ui.clear_photo(event, state)
    await ui.clear_helper(event, state)
    await state.clear()


async def _leave_reply_keyboard(event: Message | CallbackQuery, data: dict) -> None:
    """Убрать клавиатуру под полем ввода, если шаг с ней (телефон, геопозиция) был открыт."""
    if data.get(HELPER_MESSAGE_ID) is not None:
        await ui.hide_reply_keyboard(event)


async def _leave_reply_step(event: Message | CallbackQuery, state: FSMContext) -> None:
    """Шаг с кнопкой под полем ввода (геопозиция, телефон) пройден: убрать и её, и подсказку."""
    if (await state.get_data()).get(HELPER_MESSAGE_ID) is None:
        return
    await ui.clear_helper(event, state)
    await ui.hide_reply_keyboard(event)


async def _draft(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    customer: Customer,
) -> Order | None:
    """Черновик из FSM, если он всё ещё актуален.

    Неактуален — если FSM потерялся, заказ уже оформлен или клиент начал новый.
    Тогда снимаем кнопки с сообщения, по которому нажали, и просим начать заново.
    """
    data = await state.get_data()
    order_id = data.get(ORDER_ID)
    draft = await orders.get_draft(session, customer.id)
    if order_id is not None and draft is not None and draft.id == order_id:
        return draft

    if isinstance(event, CallbackQuery):
        await event.answer()
        await ui.clear_helper(event, state)
        await state.clear()
        await ui.reply(event, text=STALE_TEXT, keyboard=main_menu())
    else:
        await _reset(event, state)
        await event.answer(STALE_TEXT, reply_markup=main_menu())
    return None
