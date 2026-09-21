"""Оформление заказа: восемь шагов в одном сообщении.

Черновик живёт в базе, поэтому клиент может уйти и вернуться через день. В FSM
лежит только то, что нужно прямо сейчас: id сообщения сценария и промежуточный
выбор цветов, который ещё не записан в заказ.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import ui
from bot.callbacks import (
    ColorCB,
    ColorsDoneCB,
    ConfirmCB,
    PickupCB,
    PickupPageCB,
    PrefillCB,
    ProductCB,
    SkipCB,
    SpoonCB,
    StepCB,
    VideoCB,
)
from bot.keyboards import checkout as kb
from bot.keyboards.common import (
    BTN_CANCEL,
    BTN_ORDER,
    main_menu,
    remove_keyboard,
    request_location,
    request_phone,
    single_button,
)
from bot.states import (
    AVOID_COLORS,
    EDITING,
    FAVORITE_COLORS,
    ORDER_ID,
    PICKUP_LATITUDE,
    PICKUP_LONGITUDE,
    PICKUP_OFFSET,
    PICKUP_QUERY,
    PRODUCT_ID,
    SPOON_COUNT,
    WITH_VIDEO,
    Checkout,
)
from core.config import get_settings
from core.enums import VIDEO_ADDON_CODE, MessageTemplateKey
from core.errors import DomainError
from core.models import Customer, Order
from core.money import format_rubles
from core.services import catalog, delivery, notifications, orders, payments, settings
from core.services.catalog import RequestedItem, active_variants, price_for_spoons, spoon_options
from core.text import truncate

#: Текстовые шаги не должны перехватывать нажатие «Отменить оформление».
NOT_CANCEL = F.text != BTN_CANCEL

logger = logging.getLogger(__name__)

router = Router(name="checkout")

KIND_FAVORITE = "f"
KIND_AVOID = "a"


# --- вход в сценарий ---


@router.message(F.text == BTN_ORDER)
async def start_checkout(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    customer: Customer,
) -> None:
    """Начать оформление или предложить продолжить брошенный черновик."""
    texts = await settings.get_bot_texts(session)

    if customer.consent_accepted_at is None:
        await state.clear()
        await message.answer(
            f"{texts.consent_text}\n\nПродолжим?",
            reply_markup=single_button(
                "Согласен, продолжить",
                ConfirmCB(action="consent", value=1).pack(),
            ),
        )
        return

    draft = await orders.get_draft(session, customer.id)
    if draft is not None and draft.items:
        await state.clear()
        await state.update_data({ORDER_ID: draft.id})
        await message.answer(
            "У вас осталось незаконченное оформление.\n\n"
            f"{notifications.describe_items(draft)}\n\nПродолжим с того же места?",
            reply_markup=kb.prefill(has_previous=True),
        )
        return

    await _begin_new_order(message, state, session, customer)


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


@router.callback_query(PrefillCB.filter())
async def continue_or_restart(
    callback: CallbackQuery,
    callback_data: PrefillCB,
    state: FSMContext,
    session: AsyncSession,
    customer: Customer,
) -> None:
    """«Продолжим с того же места?» — да или начать заново."""
    await callback.answer()
    if not callback_data.use:
        await _begin_new_order(callback, state, session, customer)
        return

    draft = await orders.get_draft(session, customer.id)
    if draft is None:
        await _begin_new_order(callback, state, session, customer)
        return

    await ui.forget_ui_message(state)
    await state.update_data({ORDER_ID: draft.id})
    await _resume_draft(callback, state, session, draft)


async def _begin_new_order(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    customer: Customer,
) -> None:
    order = await orders.create_draft(session, customer)
    await state.clear()
    await state.update_data({ORDER_ID: order.id})
    await show_product_step(event, state, session)


async def _resume_draft(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    order: Order,
) -> None:
    """Вернуть клиента на первый незаполненный шаг."""
    if not order.items:
        await show_product_step(event, state, session)
    elif order.pickup_point_id is None:
        await show_pickup_search_step(event, state)
    elif not order.recipient_phone:
        await show_recipient_name_step(event, state, order)
    else:
        await show_summary(event, state, session, order)


# --- шаг 1: бокс ---


async def show_product_step(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    products = await catalog.list_active_products(session)
    if not products:
        await ui.show(
            event,
            state,
            text="Каталог сейчас обновляется. Загляните чуть позже или напишите нам.",
        )
        return

    rows = []
    for product in products:
        variants = active_variants(product.variants)
        rows.append((product, min(variant.price_kopecks for variant in variants)))

    await state.set_state(Checkout.product)
    await ui.clear_photo(event, state)
    await ui.show(
        event,
        state,
        text=ui.compose(1, "Выберите бокс", "Дальше подберём размер, цвета и пожелания."),
        keyboard=kb.products(rows),
    )


@router.callback_query(ProductCB.filter())
async def choose_product(
    callback: CallbackQuery,
    callback_data: ProductCB,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await callback.answer()
    await state.update_data({PRODUCT_ID: callback_data.product_id})
    await show_spoons_step(callback, state, session)


# --- шаг 2: ложечки ---


async def show_spoons_step(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    data = await state.get_data()
    product = await catalog.get_product(session, data[PRODUCT_ID])

    options: list[tuple[int, int]] = []
    for count in spoon_options(product, product.variants):
        _, price = price_for_spoons(product, product.variants, count)
        options.append((count, price))

    body = product.description or ""
    if product.allows_extra_spoons and product.extra_spoon_price_kopecks is not None:
        extra = format_rubles(product.extra_spoon_price_kopecks)
        body = f"{body}\n\nКаждая ложечка сверх набора — {extra}.".strip()

    await state.set_state(Checkout.spoons)
    if product.photo_file_id:
        await ui.show_photo(event, state, file_id=product.photo_file_id, caption=product.name)
    await ui.show(
        event,
        state,
        text=ui.compose(2, f"{product.name}: сколько ложечек?", body),
        keyboard=kb.spoons(options),
    )


@router.callback_query(SpoonCB.filter())
async def choose_spoons(
    callback: CallbackQuery,
    callback_data: SpoonCB,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await callback.answer()
    await state.update_data({SPOON_COUNT: callback_data.count})
    await show_video_step(callback, state, session)


# --- шаг 3: видео сборки ---


async def show_video_step(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    addon = await _video_addon(session)
    if addon is None:
        # Услуга выключена в админке — вопрос не задаём.
        await _apply_selection(event, state, session, with_video=False)
        return

    data = await state.get_data()
    await state.set_state(Checkout.video)
    await ui.clear_photo(event, state)
    await ui.show(
        event,
        state,
        text=ui.compose(
            3,
            "Хотите видео сборки?",
            "Снимем, как собираем именно ваш бокс, и пришлём вам видео.",
        ),
        keyboard=kb.video(addon.price_kopecks, chosen=data.get(WITH_VIDEO)),
    )


@router.callback_query(VideoCB.filter())
async def choose_video(
    callback: CallbackQuery,
    callback_data: VideoCB,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await callback.answer()
    await _apply_selection(callback, state, session, with_video=callback_data.enabled)


async def _apply_selection(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    *,
    with_video: bool,
) -> None:
    """Записать выбранный бокс в черновик и пересчитать сумму."""
    data = await state.get_data()
    await state.update_data({WITH_VIDEO: with_video})

    order = await orders.get_order(session, data[ORDER_ID])
    product = await catalog.get_product(session, data[PRODUCT_ID])
    spoon_count = data[SPOON_COUNT]
    variant, _ = price_for_spoons(product, product.variants, spoon_count)

    addon = await _video_addon(session)
    addon_ids = [addon.id] if (with_video and addon is not None) else []

    await orders.set_selection(
        session,
        order,
        items=[RequestedItem(variant_id=variant.id, quantity=1, spoon_count=spoon_count)],
        addon_ids=addon_ids,
    )

    if await _return_to_summary(event, state, session, order):
        return
    await show_colors_step(event, state, session, kind=KIND_FAVORITE)


async def _video_addon(session: AsyncSession):
    addons = await catalog.list_active_addons(session)
    return next((addon for addon in addons if addon.code == VIDEO_ADDON_CODE), None)


# --- шаги 4 и 5: цвета ---


async def show_colors_step(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    *,
    kind: str,
) -> None:
    palette = await catalog.list_active_colors(session)
    data = await state.get_data()
    key = FAVORITE_COLORS if kind == KIND_FAVORITE else AVOID_COLORS
    selected = data.get(key, [])

    if kind == KIND_FAVORITE:
        step, title, body, back_step = (
            4,
            "Любимые цвета",
            "Отметьте всё, что нравится. Можно выбрать несколько или дописать словами "
            "на следующем шаге.",
            "video",
        )
    else:
        step, title, body, back_step = (
            5,
            "Каких цветов лучше избегать?",
            "Если таких нет — нажмите «Пропустить».",
            "colors_f",
        )

    step_state = Checkout.favorite_colors if kind == KIND_FAVORITE else Checkout.avoid_colors
    await state.set_state(step_state)
    await ui.show(
        event,
        state,
        text=ui.compose(step, title, body),
        keyboard=kb.colors(
            palette,
            selected,
            kind=kind,
            back_step=back_step,
            skippable=True,
        ),
    )


@router.callback_query(ColorCB.filter())
async def toggle_color(
    callback: CallbackQuery,
    callback_data: ColorCB,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Отметить или снять цвет."""
    key = FAVORITE_COLORS if callback_data.kind == KIND_FAVORITE else AVOID_COLORS
    data = await state.get_data()
    selected: list[int] = list(data.get(key, []))

    if callback_data.color_id in selected:
        selected.remove(callback_data.color_id)
    else:
        selected.append(callback_data.color_id)

    await state.update_data({key: selected})
    await callback.answer()
    await show_colors_step(callback, state, session, kind=callback_data.kind)


@router.callback_query(ColorsDoneCB.filter())
async def colors_done(
    callback: CallbackQuery,
    callback_data: ColorsDoneCB,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await callback.answer()
    if callback_data.kind == KIND_FAVORITE:
        await show_colors_step(callback, state, session, kind=KIND_AVOID)
    else:
        await show_comment_step(callback, state, session)


@router.callback_query(SkipCB.filter(F.step.startswith("colors_")))
async def skip_colors(
    callback: CallbackQuery,
    callback_data: SkipCB,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    kind = callback_data.step.removeprefix("colors_")
    key = FAVORITE_COLORS if kind == KIND_FAVORITE else AVOID_COLORS
    await state.update_data({key: []})
    await callback.answer()
    if kind == KIND_FAVORITE:
        await show_colors_step(callback, state, session, kind=KIND_AVOID)
    else:
        await show_comment_step(callback, state, session)


# --- шаг 6: пожелания ---


async def show_comment_step(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    texts = await settings.get_bot_texts(session)
    await state.set_state(Checkout.comment)
    await ui.show(
        event,
        state,
        text=ui.compose(
            6,
            "Есть особые пожелания к боксу?",
            "Напишите сообщением: что положить, чего лучше не класть, для кого подарок "
            "и сколько лет получателю.\n\n"
            f"<i>{texts.wishes_disclaimer}</i>",
        ),
        keyboard=kb.comment(),
    )


@router.message(Checkout.comment, F.text, NOT_CANCEL)
async def receive_comment(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await _save_wishes(message, state, session, comment=message.text)


@router.callback_query(SkipCB.filter(F.step == "comment"))
async def skip_comment(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await callback.answer()
    await _save_wishes(callback, state, session, comment=None)


async def _save_wishes(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    *,
    comment: str | None,
) -> None:
    data = await state.get_data()
    order = await orders.get_order(session, data[ORDER_ID])
    await orders.set_wishes(
        session,
        order,
        favorite_color_ids=data.get(FAVORITE_COLORS, []),
        avoid_color_ids=data.get(AVOID_COLORS, []),
        comment=comment,
    )

    if await _return_to_summary(event, state, session, order):
        return
    await show_pickup_search_step(event, state)


# --- шаг 7: пункт выдачи ---


async def show_pickup_search_step(event: Message | CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Checkout.pickup)
    await state.update_data({PICKUP_OFFSET: 0, PICKUP_QUERY: None})
    await ui.show(
        event,
        state,
        text=ui.compose(
            7,
            "Куда доставить?",
            "Отправьте геопозицию — покажем ближайшие пункты выдачи. "
            "Или введите город и улицу.",
        ),
        keyboard=kb.pickup_search(),
    )
    chat_id = _chat_id(event)
    if chat_id is not None:
        # Геопозицию нельзя запросить инлайн-кнопкой — только reply-клавиатурой.
        await event.bot.send_message(
            chat_id=chat_id,
            text="Нажмите кнопку ниже или просто напишите адрес.",
            reply_markup=request_location(),
        )


@router.callback_query(StepCB.filter(F.step == "pickup_city"))
async def ask_city(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(Checkout.pickup)
    await ui.show(
        callback,
        state,
        text=ui.compose(7, "Куда доставить?", "Напишите город и улицу — найдём пункты выдачи."),
    )


@router.message(Checkout.pickup, F.location)
async def receive_location(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Ближайшие пункты по геопозиции. Саму точку клиента мы не храним."""
    location = message.location
    await state.update_data(
        {
            PICKUP_LATITUDE: location.latitude,
            PICKUP_LONGITUDE: location.longitude,
            PICKUP_QUERY: None,
            PICKUP_OFFSET: 0,
        },
    )
    await message.answer("Ищем ближайшие пункты выдачи…", reply_markup=remove_keyboard())
    await _show_pickup_points(message, state, session)


@router.message(Checkout.pickup, F.text, NOT_CANCEL)
async def receive_city(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    query = (message.text or "").strip()
    if len(query) < 2:
        await message.answer("Напишите город или улицу целиком — так найдём точнее.")
        return

    await state.update_data(
        {PICKUP_QUERY: query, PICKUP_LATITUDE: None, PICKUP_LONGITUDE: None, PICKUP_OFFSET: 0},
    )
    await message.answer("Ищем пункты выдачи…", reply_markup=remove_keyboard())
    await _show_pickup_points(message, state, session)


@router.callback_query(PickupPageCB.filter())
async def paginate_points(
    callback: CallbackQuery,
    callback_data: PickupPageCB,
    state: FSMContext,
    session: AsyncSession,
) -> None:
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
        points = await delivery.search_by_city(
            session,
            query,
            environment=environment,
            limit=page + 1,
            offset=offset,
        )
    else:
        await show_pickup_search_step(event, state)
        return

    has_more = not by_location and len(points) > page
    points = points[:page]

    if not points:
        await ui.show(
            event,
            state,
            text=ui.compose(
                7,
                "Пункты выдачи не найдены",
                "Попробуйте написать другой город или отправить геопозицию.",
            ),
            keyboard=kb.pickup_search(),
        )
        return

    lines = ["Выберите пункт выдачи:", ""]
    for point in points:
        distance = distances.get(point.id)
        suffix = f" · {distance:.1f} км" if distance is not None else ""
        lines.append(f"📍 <b>{truncate(point.address, 80)}</b>{suffix}")
        if point.working_hours:
            lines.append(f"   {point.working_hours}")
    lines.append("")
    lines.append(f"Доставка: {await _delivery_service_names(session)}")

    await ui.show(
        event,
        state,
        text=ui.compose(7, "Куда доставить?", "\n".join(lines)),
        keyboard=kb.pickup_points(points, offset=offset, has_more=has_more),
    )


async def _delivery_service_names(session: AsyncSession) -> str:
    providers = await delivery.enabled_providers(session)
    return ", ".join(provider.name for provider in providers) or "уточним при сборке"


@router.callback_query(PickupCB.filter())
async def choose_pickup_point(
    callback: CallbackQuery,
    callback_data: PickupCB,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Сохранить выбранный пункт, показать его на карте и посчитать доставку."""
    await callback.answer()
    data = await state.get_data()
    order = await orders.get_order(session, data[ORDER_ID])
    point = await delivery.get_pickup_point(session, callback_data.point_id)

    parcel = delivery.parcel_for_order(order)
    quote = await delivery.quote(session, point=point, parcel=parcel)
    await orders.set_pickup_point(
        session,
        order,
        point=point,
        delivery_kopecks=quote.price_kopecks,
        provider_name=quote.provider_name,
        is_fallback_price=quote.is_fallback,
    )

    if callback.message is not None:
        await callback.bot.send_venue(
            chat_id=callback.message.chat.id,
            latitude=point.latitude,
            longitude=point.longitude,
            title=point.name,
            address=point.address,
        )

    if await _return_to_summary(callback, state, session, order):
        return
    await show_recipient_name_step(callback, state, order)


# --- шаг 8: контакты ---


async def show_recipient_name_step(
    event: Message | CallbackQuery,
    state: FSMContext,
    order: Order,
) -> None:
    await state.set_state(Checkout.recipient_name)
    current = order.recipient_name or ""
    hint = f"\n\nСейчас указано: <b>{current}</b>" if current else ""
    await ui.show(
        event,
        state,
        text=ui.compose(
            8,
            "Как зовут получателя?",
            f"Напишите имя — его увидит курьер и пункт выдачи.{hint}",
        ),
    )


@router.message(Checkout.recipient_name, F.text, NOT_CANCEL)
async def receive_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if len(name) < 2:
        await message.answer("Имя слишком короткое, напишите полностью.")
        return

    await state.update_data({"recipient_name": name})
    await state.set_state(Checkout.recipient_phone)
    await ui.show(
        message,
        state,
        text=ui.compose(
            8,
            "Телефон получателя",
            "Нажмите кнопку ниже — телефон подставится сам. "
            "Он нужен службе доставки, чтобы сообщить о посылке.",
        ),
    )
    await message.answer("Телефон:", reply_markup=request_phone())


@router.message(Checkout.recipient_phone, F.contact)
async def receive_contact(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    customer: Customer,
) -> None:
    await _save_phone(message, state, session, customer, phone=message.contact.phone_number)


@router.message(Checkout.recipient_phone, F.text, NOT_CANCEL)
async def receive_phone_text(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    customer: Customer,
) -> None:
    phone = (message.text or "").strip()
    digits = "".join(character for character in phone if character.isdigit())
    if len(digits) < 10:
        await message.answer("Телефон выглядит неполным. Пример: +7 900 123-45-67")
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
    data = await state.get_data()
    order = await orders.get_order(session, data[ORDER_ID])
    await orders.set_recipient(
        session,
        order,
        name=data.get("recipient_name") or order.recipient_name or customer.display_name,
        phone=phone,
        email=order.recipient_email,
        customer=customer,
    )

    await message.answer("Телефон сохранён.", reply_markup=main_menu())
    await ui.forget_ui_message(state)
    await state.set_state(Checkout.recipient_email)
    await ui.show(
        message,
        state,
        text=ui.compose(
            8,
            "Email для чека",
            "Пришлём на него чек об оплате. Можно пропустить — тогда чек придёт на телефон.",
        ),
        keyboard=kb.email_step(),
    )


@router.message(Checkout.recipient_email, F.text, NOT_CANCEL)
async def receive_email(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    customer: Customer,
) -> None:
    email = (message.text or "").strip()
    if "@" not in email or "." not in email.split("@")[-1]:
        await message.answer("Похоже, в адресе опечатка. Пример: anna@mail.ru")
        return

    data = await state.get_data()
    order = await orders.get_order(session, data[ORDER_ID])
    await orders.set_recipient(
        session,
        order,
        name=order.recipient_name or customer.display_name,
        phone=order.recipient_phone or "",
        email=email,
        customer=customer,
    )
    await show_summary(message, state, session, order)


@router.callback_query(SkipCB.filter(F.step == "email"))
async def skip_email(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await callback.answer()
    data = await state.get_data()
    order = await orders.get_order(session, data[ORDER_ID])
    await show_summary(callback, state, session, order)


# --- итог ---


async def show_summary(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    order: Order,
) -> None:
    await state.set_state(Checkout.summary)
    await state.update_data({EDITING: False})
    await ui.clear_photo(event, state)
    await ui.show(
        event,
        state,
        text=_summary_text(order),
        keyboard=kb.summary(),
    )


def _summary_text(order: Order) -> str:
    snapshot = order.pickup_snapshot or {}
    lines = [
        notifications.describe_items(order),
        "",
        f"Боксы и услуги: {format_rubles(order.subtotal_kopecks)}",
        f"Доставка: {format_rubles(order.delivery_kopecks)}",
        f"<b>ИТОГО: {format_rubles(order.total_kopecks)}</b>",
        "",
        f"Получатель: {order.recipient_name}, {order.recipient_phone}",
    ]
    if snapshot:
        lines.append(f"Пункт выдачи: {snapshot.get('address', '')}")
        lines.append(f"Служба: {snapshot.get('provider_name', '')}")
    if order.customer_comment:
        lines.append(f"Пожелания: {truncate(order.customer_comment, 200)}")
    return ui.compose(None, "Проверьте заказ", "\n".join(lines))


@router.callback_query(StepCB.filter(F.step == "pay"))
async def pay(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Оформить заказ и выдать ссылку на оплату."""
    data = await state.get_data()
    order = await orders.get_order(session, data[ORDER_ID])

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
    await ui.show(
        callback,
        state,
        text=text or f"Заказ {order.display_number} оформлен, ждём оплату.",
        keyboard=kb.payment(url, order.id),
    )
    await state.clear()


# --- навигация и отмена ---


@router.callback_query(StepCB.filter())
async def navigate(
    callback: CallbackQuery,
    callback_data: StepCB,
    state: FSMContext,
    session: AsyncSession,
    customer: Customer,
) -> None:
    """Кнопки «← Назад» и «✏️ Изменить …»."""
    step = callback_data.step
    await callback.answer()

    if step == "cancel":
        await _cancel_checkout(callback, state, session, customer)
        return

    data = await state.get_data()
    # Правка из итога возвращает обратно в итог, а не гонит по всем шагам заново.
    if await state.get_state() == Checkout.summary.state:
        await state.update_data({EDITING: True})

    if step == "product":
        await show_product_step(callback, state, session)
    elif step == "spoons":
        await show_spoons_step(callback, state, session)
    elif step == "video":
        await show_video_step(callback, state, session)
    elif step == "colors_f":
        await show_colors_step(callback, state, session, kind=KIND_FAVORITE)
    elif step == "colors_a":
        await show_colors_step(callback, state, session, kind=KIND_AVOID)
    elif step == "comment":
        await show_comment_step(callback, state, session)
    elif step == "pickup_search":
        await show_pickup_search_step(callback, state)
    elif step == "recipient":
        order = await orders.get_order(session, data[ORDER_ID])
        await show_recipient_name_step(callback, state, order)


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

    await ui.clear_photo(event, state)
    await state.clear()

    chat_id = _chat_id(event)
    if chat_id is not None:
        await event.bot.send_message(
            chat_id=chat_id,
            text="Оформление отменено. Если что — начнём заново 💗",
            reply_markup=main_menu(),
        )


def _chat_id(event: Message | CallbackQuery) -> int | None:
    """Чат события — у сообщения и у нажатия кнопки он достаётся по-разному."""
    if isinstance(event, Message):
        return event.chat.id
    return event.message.chat.id if event.message is not None else None


async def _return_to_summary(
    event: Message | CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    order: Order,
) -> bool:
    """Если клиент правил один раздел из итога — вернуть его в итог."""
    data = await state.get_data()
    if not data.get(EDITING):
        return False
    await show_summary(event, state, session, order)
    return True
