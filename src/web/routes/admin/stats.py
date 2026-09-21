"""Статистика за период."""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Query, Request

from core.labels import delivery_provider_label
from core.services import stats as stats_service
from web.security import CurrentAdmin, DbSession, csrf_token
from web.templating import render

router = APIRouter(prefix="/stats", tags=["admin"])


@router.get("")
async def stats_page(
    request: Request,
    session: DbSession,
    admin: CurrentAdmin,
    period: Annotated[str, Query()] = "month",
    since: Annotated[str, Query()] = "",
    until: Annotated[str, Query()] = "",
):
    start, end = stats_service.period_range(
        period,
        since=_as_date(since),
        until=_as_date(until),
    )
    data = await stats_service.collect(session, since=start, until=end)

    return render(
        request,
        "admin/stats.html",
        {
            "stats": data,
            "period": period,
            "periods": stats_service.PERIOD_LABELS,
            "provider_labels": {
                code: delivery_provider_label(code) for code in data.by_delivery_provider
            },
            "admin": admin,
            "csrf_token": csrf_token(request),
        },
    )


def _as_date(raw: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(raw) if raw else None
    except ValueError:
        return None
