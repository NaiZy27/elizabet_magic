"""Статистика за период."""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Query, Request

from core.clock import today_moscow
from core.labels import delivery_provider_label
from core.services import analytics
from core.services import stats as stats_service
from web.security import CurrentOwner, DbSession, csrf_token
from web.templating import render

router = APIRouter(prefix="/stats", tags=["admin"])

#: Быстрые периоды над графиками. «Свой период» выбирается отдельно, датами.
PERIODS: dict[str, str] = {
    "week": "Неделя",
    "month": "Месяц",
    "quarter": "3 месяца",
}


@router.get("")
async def stats_page(
    request: Request,
    session: DbSession,
    admin: CurrentOwner,
    period: Annotated[str, Query()] = "month",
    since: Annotated[str, Query()] = "",
    until: Annotated[str, Query()] = "",
):
    if period == "quarter":
        # Три месяца — чтобы видеть сезонность, а не только последний месяц.
        end = today_moscow()
        start = end - dt.timedelta(days=89)
    else:
        start, end = stats_service.period_range(
            period,
            since=_as_date(since),
            until=_as_date(until),
        )
    data = await stats_service.collect(session, since=start, until=end)
    trend = await analytics.sales_trend(session, since=start, until=end)

    return render(
        request,
        "admin/stats.html",
        {
            "stats": data,
            "period": period,
            "periods": PERIODS,
            "trend": trend,
            "delta": analytics.delta,
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
