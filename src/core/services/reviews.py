"""Отзывы: сохранить, пролистать, отметить публикацию в канале."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.clock import now_utc
from core.errors import NotFoundError, ValidationError
from core.models import Customer, Review

#: Отзыв длиннее не влезет в подпись к фото в канале (1024 символа) вместе с подписью автора.
MAX_REVIEW_LENGTH = 900


async def create(
    session: AsyncSession,
    customer: Customer,
    *,
    text: str,
    photo_file_id: str | None = None,
    order_id: int | None = None,
) -> Review:
    text = (text or "").strip()
    if not text and not photo_file_id:
        raise ValidationError("Напишите пару слов или пришлите фото")
    review = Review(
        customer_id=customer.id,
        order_id=order_id,
        text=text[:MAX_REVIEW_LENGTH],
        photo_file_id=photo_file_id,
    )
    session.add(review)
    await session.flush()
    return review


async def count_visible(session: AsyncSession) -> int:
    total = await session.scalar(select(func.count(Review.id)).where(Review.hidden_at.is_(None)))
    return int(total or 0)


async def visible_at(session: AsyncSession, index: int) -> Review | None:
    """Отзыв по порядку, от свежих к старым. Скрытые не показываем."""
    return await session.scalar(
        select(Review)
        .options(selectinload(Review.customer))
        .where(Review.hidden_at.is_(None))
        .order_by(Review.created_at.desc(), Review.id.desc())
        .offset(max(0, index))
        .limit(1),
    )


async def get(session: AsyncSession, review_id: int) -> Review:
    review = await session.scalar(
        select(Review).options(selectinload(Review.customer)).where(Review.id == review_id),
    )
    if review is None:
        raise NotFoundError("Отзыв не найден")
    return review


async def hide(session: AsyncSession, review: Review) -> None:
    review.hidden_at = now_utc()
    await session.flush()


async def mark_posted(session: AsyncSession, review: Review) -> None:
    review.posted_at = now_utc()
    await session.flush()


def author(review: Review) -> str:
    """Подпись под отзывом: имя без фамилии и без @username — это публикуется в канале."""
    name = (review.customer.full_name or "").strip() if review.customer else ""
    return name.split()[0] if name else "Покупательница"


def channel_text(review: Review) -> str:
    """Как отзыв выглядит в канале."""
    body = f"«{review.text}»\n\n" if review.text else ""
    return f"💌 Отзыв\n\n{body}— {author(review)}"
