"""Отзывы в панели и фото из Telegram.

Отзывы клиенты оставляют в боте. Здесь владелица видит их все разом, публикует
понравившиеся в канал и скрывает лишние — то же, что в рабочем режиме бота,
только удобнее на большом экране.

Фото (отзывов и боксов) хранятся в Telegram, у нас только file_id. Панель
получает картинку через бота и отдаёт её браузеру; недавние держим в памяти.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from core.config import get_settings
from core.errors import NotFoundError
from core.models import Product, Review
from core.services import reviews as reviews_service
from web.security import CurrentAdmin, CurrentOwner, DbSession, csrf_token, verify_csrf
from web.templating import render

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin"])

#: Какие отзывы показывать: новые (не в канале), опубликованные, скрытые.
FILTERS = {"new": "Новые", "posted": "В канале", "hidden": "Скрытые"}

_PHOTO_CACHE: OrderedDict[str, bytes] = OrderedDict()
_PHOTO_CACHE_SIZE = 80


@router.get("/reviews")
async def reviews_page(
    request: Request,
    session: DbSession,
    admin: CurrentOwner,
    show: Annotated[str, Query()] = "new",
):
    show = show if show in FILTERS else "new"
    statement = select(Review).options(selectinload(Review.customer), selectinload(Review.order))
    if show == "hidden":
        statement = statement.where(Review.hidden_at.is_not(None))
    elif show == "posted":
        statement = statement.where(Review.hidden_at.is_(None), Review.posted_at.is_not(None))
    else:
        statement = statement.where(Review.hidden_at.is_(None), Review.posted_at.is_(None))
    rows = list(await session.scalars(statement.order_by(Review.created_at.desc()).limit(120)))

    counts_rows = (
        await session.execute(
            select(
                func.count().filter(Review.hidden_at.is_(None), Review.posted_at.is_(None)),
                func.count().filter(Review.hidden_at.is_(None), Review.posted_at.is_not(None)),
                func.count().filter(Review.hidden_at.is_not(None)),
            ),
        )
    ).one()
    counts = dict(zip(FILTERS, (int(value or 0) for value in counts_rows), strict=True))

    return render(
        request,
        "admin/reviews.html",
        {
            "reviews": rows,
            "show": show,
            "filters": FILTERS,
            "counts": counts,
            "author": reviews_service.author,
            "channel_ready": bool(get_settings().owner.reviews_channel_id),
            "admin": admin,
            "csrf_token": csrf_token(request),
        },
    )


@router.post("/reviews/{review_id}/post")
async def post_review(
    request: Request,
    review_id: int,
    session: DbSession,
    admin: CurrentOwner,
    csrf: Annotated[str, Form(alias="csrf_token")] = "",
):
    """Опубликовать отзыв в канал от имени бота."""
    verify_csrf(request, csrf)
    channel = get_settings().owner.reviews_channel_id
    if not channel:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Канал для отзывов не подключён: добавьте бота в канал администратором.",
        )
    review = await _get_review(session, review_id)

    from worker.telegram import get_bot

    chat_id: int | str = int(channel) if channel.lstrip("-").isdigit() else channel
    text = reviews_service.channel_text(review)
    try:
        if review.photo_file_id:
            await get_bot().send_photo(chat_id=chat_id, photo=review.photo_file_id, caption=text)
        else:
            await get_bot().send_message(chat_id=chat_id, text=text)
    except Exception as error:
        logger.warning("Отзыв %s не ушёл в канал: %s", review.id, error)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Telegram не принял публикацию: проверьте, что бот — администратор канала.",
        ) from error
    await reviews_service.mark_posted(session, review)
    return RedirectResponse("/admin/reviews?show=new", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/reviews/{review_id}/hide")
async def hide_review(
    request: Request,
    review_id: int,
    session: DbSession,
    admin: CurrentOwner,
    csrf: Annotated[str, Form(alias="csrf_token")] = "",
):
    verify_csrf(request, csrf)
    review = await _get_review(session, review_id)
    await reviews_service.hide(session, review)
    return RedirectResponse("/admin/reviews", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/reviews/{review_id}/restore")
async def restore_review(
    request: Request,
    review_id: int,
    session: DbSession,
    admin: CurrentOwner,
    csrf: Annotated[str, Form(alias="csrf_token")] = "",
):
    verify_csrf(request, csrf)
    review = await _get_review(session, review_id)
    review.hidden_at = None
    await session.flush()
    return RedirectResponse("/admin/reviews?show=hidden", status_code=status.HTTP_303_SEE_OTHER)


async def _get_review(session, review_id: int) -> Review:
    try:
        return await reviews_service.get(session, review_id)
    except NotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=error.message) from error


@router.get("/media/{kind}/{object_id}")
async def media(kind: str, object_id: int, session: DbSession, admin: CurrentAdmin):
    """Картинка из Telegram по file_id отзыва или бокса."""
    if kind == "review":
        file_id = await session.scalar(select(Review.photo_file_id).where(Review.id == object_id))
    elif kind == "product":
        file_id = await session.scalar(select(Product.photo_file_id).where(Product.id == object_id))
    else:
        file_id = None
    if not file_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Фото нет")

    content = _PHOTO_CACHE.get(file_id)
    if content is None:
        from worker.telegram import get_bot

        try:
            bot = get_bot()
            file = await bot.get_file(file_id)
            buffer = await bot.download_file(file.file_path)
            content = buffer.read() if buffer else b""
        except Exception as error:
            logger.warning("Не удалось получить фото %s/%s: %s", kind, object_id, error)
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Фото недоступно") from error
        _PHOTO_CACHE[file_id] = content
        while len(_PHOTO_CACHE) > _PHOTO_CACHE_SIZE:
            _PHOTO_CACHE.popitem(last=False)
    else:
        _PHOTO_CACHE.move_to_end(file_id)

    return Response(
        content,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=86400"},
    )
