"""Приём заказов: открыт ли он сейчас и когда откроется.

Два ограничителя, оба настраиваются в панели:
- пауза — владелица закрыла приём (отпуск), можно с датой автооткрытия;
- лимит — в работе одновременно не больше N заказов. В работе — это оплаченные
  в очереди и на сборке плюс те, что ждут оплаты: иначе в момент оплаты лимит
  мог бы оказаться превышен.

Когда лимит заполнен, называем дату, к которой освободится место: для этого та же
очередь, по которой считаются даты готовности на доске.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.clock import format_date, today_moscow
from core.enums import QUEUE_STATUSES, OrderStatus
from core.errors import ValidationError
from core.models import Order
from core.schemas.settings import IntakeSettings
from core.services import board, settings
from core.workdays import WorkingCalendar, next_working_day, shift_working_days

#: Заказы, которые занимают место в лимите.
LOAD_STATUSES: frozenset[OrderStatus] = frozenset({*QUEUE_STATUSES, OrderStatus.WAITING_PAYMENT})


@dataclass(frozen=True, slots=True)
class IntakeState:
    """Можно ли сейчас оформить заказ, и если нет — что сказать клиенту."""

    is_open: bool
    message: str = ""
    #: Ближайшая дата, когда появится место (если закрыто по лимиту).
    next_date: dt.date | None = None


async def current_load(session: AsyncSession) -> int:
    """Сколько заказов сейчас занимают место в лимите."""
    count = await session.scalar(
        select(func.count(Order.id)).where(Order.status.in_(tuple(LOAD_STATUSES))),
    )
    return int(count or 0)


def free_slot_date(
    ready_dates: list[dt.date],
    *,
    load: int,
    limit: int,
    calendar: WorkingCalendar,
) -> dt.date | None:
    """Когда освободится место при заполненном лимите.

    `ready_dates` — даты готовности заказов очереди по порядку. Чтобы влез ещё один
    заказ, должны закончиться (load - limit + 1) заказов; место появится на
    следующий рабочий день после готовности последнего из них.
    """
    need_to_finish = load - limit + 1
    if need_to_finish <= 0:
        return None
    ordered = sorted(ready_dates)
    if need_to_finish > len(ordered):
        # Места держат неоплаченные заказы: они освободятся по сроку оплаты,
        # а не по календарю сборки.
        return None
    return next_working_day(ordered[need_to_finish - 1], calendar)


async def check(session: AsyncSession, *, today: dt.date | None = None) -> IntakeState:
    """Открыт ли приём прямо сейчас."""
    day = today or today_moscow()
    intake = await settings.get_intake(session)

    if intake.is_paused(day):
        message = intake.pause_message
        if intake.resume_on is not None:
            message = f"{message}\n\nПриём откроется {format_date(intake.resume_on)}."
        return IntakeState(is_open=False, message=message, next_date=intake.resume_on)

    if intake.queue_limit is None:
        return IntakeState(is_open=True)

    load = await current_load(session)
    if load < intake.queue_limit:
        return IntakeState(is_open=True)

    calendar = await settings.get_working_calendar(session)
    ready = await board.ready_dates_for_queue(session, calendar=calendar, today=day)
    next_date = free_slot_date(
        list(ready.values()),
        load=load,
        limit=intake.queue_limit,
        calendar=calendar,
    )
    if next_date is None:
        message = (
            "Заказы на ближайшие дни закончились. Загляните через пару часов — "
            "места могут освободиться."
        )
    else:
        message = (
            "Заказы на ближайшую дату закончились. "
            f"Ближайшая доступная дата — {format_date(next_date)}. "
            "Возвращайтесь к этому дню — будем рады собрать для вас бокс 💗"
        )
    return IntakeState(is_open=False, message=message, next_date=next_date)


async def ensure_open(session: AsyncSession) -> None:
    """Проверка перед оформлением: закрыто — ошибка с текстом для клиента."""
    state = await check(session)
    if not state.is_open:
        raise ValidationError(state.message)


def promised_date(
    ready_date: dt.date | None,
    *,
    intake: IntakeSettings,
    calendar: WorkingCalendar,
) -> dt.date | None:
    """Дата для клиента: расчётная готовность плюс запас из настроек."""
    if ready_date is None or intake.extra_lead_days <= 0:
        return ready_date
    return shift_working_days(ready_date, intake.extra_lead_days, calendar)
