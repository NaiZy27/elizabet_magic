"""Каталог: боксы, варианты, доп. услуги и палитра цветов.

Цены вводятся в рублях и тут же переводятся в копейки — в базе плавающих чисел нет.
Фото загружается файлом: сервер отправляет его клиентским ботом в служебный чат
и запоминает file_id, руками его никто не вводит.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from core.config import get_settings
from core.enums import AddonChargeMode
from core.models import Addon, Color, Product, ProductVariant
from core.money import parse_rubles
from core.services import catalog as catalog_service
from web.security import CurrentOwner, DbSession, csrf_token, verify_csrf
from web.templating import render

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/catalog", tags=["admin"])

#: Телеграм не принимает файлы крупнее 10 МБ по этому методу.
MAX_PHOTO_BYTES = 10 * 1024 * 1024


@router.get("")
async def catalog_page(request: Request, session: DbSession, admin: CurrentOwner):
    products = await catalog_service.list_products_for_admin(session)
    addons = await session.scalars(select(Addon).order_by(Addon.sort_order, Addon.id))
    colors = await session.scalars(select(Color).order_by(Color.sort_order, Color.id))

    return render(
        request,
        "admin/catalog.html",
        {
            "products": products,
            "addons": list(addons),
            "colors": list(colors),
            "admin": admin,
            "csrf_token": csrf_token(request),
            "charge_modes": list(AddonChargeMode),
        },
    )


@router.get("/products/new")
async def new_product(request: Request, session: DbSession, admin: CurrentOwner):
    return render(
        request,
        "admin/product_form.html",
        {"product": None, "admin": admin, "csrf_token": csrf_token(request)},
    )


@router.get("/products/{product_id}")
async def edit_product(
    request: Request,
    product_id: int,
    session: DbSession,
    admin: CurrentOwner,
):
    product = await _get_product(session, product_id)
    return render(
        request,
        "admin/product_form.html",
        {
            "product": product,
            "variants": catalog_service.active_variants(product.variants),
            "all_variants": sorted(product.variants, key=lambda item: item.sort_order),
            "admin": admin,
            "csrf_token": csrf_token(request),
        },
    )


@router.post("/products")
async def save_product(
    request: Request,
    session: DbSession,
    admin: CurrentOwner,
    name: Annotated[str, Form()],
    sku: Annotated[str, Form()],
    production_days: Annotated[int, Form(ge=1, le=60)],
    product_id: Annotated[int, Form()] = 0,
    description: Annotated[str, Form()] = "",
    extra_spoon_price: Annotated[str, Form()] = "",
    max_spoon_count: Annotated[str, Form()] = "",
    sort_order: Annotated[int, Form()] = 0,
    is_active: Annotated[bool, Form()] = False,
    csrf: Annotated[str, Form(alias="csrf_token")] = "",
):
    verify_csrf(request, csrf)

    extra_price = _optional_price(extra_spoon_price, "Цена дополнительной ложечки")
    max_spoons = _optional_int(max_spoon_count, "Максимум ложечек")
    if (extra_price is None) != (max_spoons is None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Для докупки ложечек укажите и цену, и максимум — или оставьте оба поля пустыми",
        )

    if product_id:
        product = await _get_product(session, product_id)
    else:
        product = Product(sku=sku.strip())
        session.add(product)

    product.name = name.strip()
    product.sku = sku.strip()
    product.description = description.strip() or None
    product.production_days = production_days
    product.extra_spoon_price_kopecks = extra_price
    product.max_spoon_count = max_spoons
    product.sort_order = sort_order
    product.is_active = is_active

    await session.flush()
    return RedirectResponse(
        f"/admin/catalog/products/{product.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/products/{product_id}/photo")
async def upload_photo(
    request: Request,
    product_id: int,
    session: DbSession,
    admin: CurrentOwner,
    photo: Annotated[UploadFile, File()],
    csrf: Annotated[str, Form(alias="csrf_token")] = "",
):
    """Отправляем фото ботом в служебный чат и запоминаем file_id."""
    verify_csrf(request, csrf)
    settings = get_settings()
    if settings.owner.service_chat_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Не задан служебный чат (SERVICE_CHAT_ID) — фото загрузить некуда",
        )

    content = await photo.read()
    if not content:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Файл пустой",
        )
    if len(content) > MAX_PHOTO_BYTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Файл больше 10 МБ — уменьшите картинку",
        )

    product = await _get_product(session, product_id)

    # Импорт внутри: веб-процесс поднимает бота только ради загрузки фото.
    from aiogram.types import BufferedInputFile

    from worker.telegram import get_bot

    try:
        message = await get_bot().send_photo(
            chat_id=settings.owner.service_chat_id,
            photo=BufferedInputFile(content, filename=photo.filename or "photo.jpg"),
            caption=f"Фото для бокса «{product.name}»",
        )
    except Exception as error:
        logger.exception("Не удалось загрузить фото бокса")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Telegram не принял файл. Попробуйте ещё раз.",
        ) from error

    if not message.photo:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Telegram не вернул файл — попробуйте другую картинку",
        )

    product.photo_file_id = message.photo[-1].file_id
    await session.flush()
    return RedirectResponse(
        f"/admin/catalog/products/{product_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/products/{product_id}/variants")
async def save_variant(
    request: Request,
    product_id: int,
    session: DbSession,
    admin: CurrentOwner,
    name: Annotated[str, Form()],
    sku: Annotated[str, Form()],
    spoon_count: Annotated[int, Form(ge=1, le=100)],
    price: Annotated[str, Form()],
    variant_id: Annotated[int, Form()] = 0,
    weight_g: Annotated[str, Form()] = "",
    length_cm: Annotated[str, Form()] = "",
    width_cm: Annotated[str, Form()] = "",
    height_cm: Annotated[str, Form()] = "",
    sort_order: Annotated[int, Form()] = 0,
    is_active: Annotated[bool, Form()] = False,
    csrf: Annotated[str, Form(alias="csrf_token")] = "",
):
    verify_csrf(request, csrf)
    await _get_product(session, product_id)

    if variant_id:
        variant = await session.get(ProductVariant, variant_id)
        if variant is None or variant.product_id != product_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Вариант не найден")
    else:
        variant = ProductVariant(product_id=product_id)
        session.add(variant)

    variant.name = name.strip()
    variant.sku = sku.strip()
    variant.spoon_count = spoon_count
    variant.price_kopecks = _price(price, "Цена")
    variant.weight_g = _optional_int(weight_g, "Вес")
    variant.length_cm = _optional_int(length_cm, "Длина")
    variant.width_cm = _optional_int(width_cm, "Ширина")
    variant.height_cm = _optional_int(height_cm, "Высота")
    variant.sort_order = sort_order
    variant.is_active = is_active

    await session.flush()
    return RedirectResponse(
        f"/admin/catalog/products/{product_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/addons")
async def save_addon(
    request: Request,
    session: DbSession,
    admin: CurrentOwner,
    name: Annotated[str, Form()],
    code: Annotated[str, Form()],
    price: Annotated[str, Form()],
    charge_mode: Annotated[str, Form()],
    addon_id: Annotated[int, Form()] = 0,
    extra_production_days: Annotated[int, Form(ge=0, le=30)] = 0,
    sort_order: Annotated[int, Form()] = 0,
    is_active: Annotated[bool, Form()] = False,
    csrf: Annotated[str, Form(alias="csrf_token")] = "",
):
    verify_csrf(request, csrf)
    if addon_id:
        addon = await session.get(Addon, addon_id)
        if addon is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Услуга не найдена")
    else:
        addon = Addon(code=code.strip())
        session.add(addon)

    addon.name = name.strip()
    addon.code = code.strip()
    addon.price_kopecks = _price(price, "Цена")
    addon.charge_mode = AddonChargeMode(charge_mode)
    addon.extra_production_days = extra_production_days
    addon.sort_order = sort_order
    addon.is_active = is_active

    await session.flush()
    return RedirectResponse("/admin/catalog", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/colors")
async def save_color(
    request: Request,
    session: DbSession,
    admin: CurrentOwner,
    name: Annotated[str, Form()],
    color_id: Annotated[int, Form()] = 0,
    sort_order: Annotated[int, Form()] = 0,
    is_active: Annotated[bool, Form()] = False,
    csrf: Annotated[str, Form(alias="csrf_token")] = "",
):
    verify_csrf(request, csrf)
    if color_id:
        color = await session.get(Color, color_id)
        if color is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Цвет не найден")
    else:
        color = Color(name=name.strip())
        session.add(color)

    color.name = name.strip()
    color.sort_order = sort_order
    color.is_active = is_active

    await session.flush()
    return RedirectResponse("/admin/catalog", status_code=status.HTTP_303_SEE_OTHER)


async def _get_product(session, product_id: int) -> Product:
    product = await session.scalar(
        select(Product).where(Product.id == product_id),
    )
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Бокс не найден")
    await session.refresh(product, ["variants"])
    return product


def _price(raw: str, field: str) -> int:
    try:
        return parse_rubles(raw)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field}: {error}",
        ) from error


def _optional_price(raw: str, field: str) -> int | None:
    return _price(raw, field) if raw.strip() else None


def _optional_int(raw: str, field: str) -> int | None:
    value = raw.strip()
    if not value:
        return None
    if not value.isdigit() or int(value) <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field}: укажите целое число больше нуля",
        )
    return int(value)
