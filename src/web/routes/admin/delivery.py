"""Службы доставки и справочник пунктов выдачи."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_, select

from core.enums import DeliveryProviderCode, ProviderEnvironment
from core.config import get_settings
from core.models import DeliveryProvider, PickupPoint
from core.money import parse_rubles
from web.security import CurrentAdmin, DbSession, csrf_token, verify_csrf
from web.templating import render

router = APIRouter(tags=["admin"])

PAGE_SIZE = 30


@router.get("/delivery")
async def delivery_page(request: Request, session: DbSession, admin: CurrentAdmin):
    providers = await session.scalars(select(DeliveryProvider).order_by(DeliveryProvider.id))
    providers = list(providers)

    counts = {}
    for provider in providers:
        counts[provider.code] = await session.scalar(
            select(func.count())
            .select_from(PickupPoint)
            .where(
                PickupPoint.provider == provider.code,
                PickupPoint.is_active.is_(True),
                PickupPoint.environment == get_settings().delivery_environment,
            ),
        )

    return render(
        request,
        "admin/delivery.html",
        {
            "providers": providers,
            "point_counts": counts,
            "environment": get_settings().delivery_environment,
            "admin": admin,
            "csrf_token": csrf_token(request),
        },
    )


@router.post("/delivery/{provider_id}")
async def save_provider(
    request: Request,
    provider_id: int,
    session: DbSession,
    admin: CurrentAdmin,
    fallback_price: Annotated[str, Form()],
    is_enabled: Annotated[bool, Form()] = False,
    csrf: Annotated[str, Form(alias="csrf_token")] = "",
):
    """Включение службы и резервная цена, по которой считаем, если API молчит."""
    verify_csrf(request, csrf)
    provider = await session.get(DeliveryProvider, provider_id)
    if provider is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Служба не найдена")

    try:
        provider.fallback_price_kopecks = parse_rubles(fallback_price)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Резервная цена: {error}",
        ) from error

    provider.is_enabled = is_enabled
    await session.flush()
    return RedirectResponse("/admin/delivery", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/pickup-points")
async def pickup_points_page(
    request: Request,
    session: DbSession,
    admin: CurrentAdmin,
    q: Annotated[str, Query()] = "",
    provider: Annotated[str, Query()] = "",
    only_active: Annotated[bool, Query()] = True,
    page: Annotated[int, Query(ge=1)] = 1,
):
    statement = select(PickupPoint).order_by(PickupPoint.city, PickupPoint.address)

    query = q.strip()
    if query:
        pattern = f"%{query.lower()}%"
        statement = statement.where(
            or_(
                func.lower(PickupPoint.city).like(pattern),
                func.lower(PickupPoint.address).like(pattern),
                func.lower(PickupPoint.external_id).like(pattern),
            ),
        )
    if provider:
        statement = statement.where(PickupPoint.provider == provider)
    if only_active:
        statement = statement.where(PickupPoint.is_active.is_(True))

    total = await session.scalar(
        select(func.count()).select_from(statement.order_by(None).subquery()),
    )
    rows = await session.scalars(statement.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE))

    return render(
        request,
        "admin/pickup_points.html",
        {
            "points": list(rows),
            "q": query,
            "provider": provider,
            "only_active": only_active,
            "providers": list(DeliveryProviderCode),
            "environments": list(ProviderEnvironment),
            "page": page,
            "pages": max(1, ((total or 0) + PAGE_SIZE - 1) // PAGE_SIZE),
            "total": total or 0,
            "admin": admin,
            "csrf_token": csrf_token(request),
        },
    )
