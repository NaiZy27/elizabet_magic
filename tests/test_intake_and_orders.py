"""Приём заказов, номер до оплаты и проверки получателя — без базы."""

from __future__ import annotations

import datetime as dt

from core.enums import CancelReason, OrderStatus
from core.models import Order
from core.schemas.settings import IntakeSettings
from core.services.intake import free_slot_date, promised_date
from core.services.orders import can_revive, is_valid_email


def test_free_slot_after_enough_orders_finish(workdays, monday):
    ready = [monday, monday + dt.timedelta(days=1), monday + dt.timedelta(days=2)]

    # Лимит 3 и 3 в работе: место появится после первого заказа — во вторник.
    assert free_slot_date(ready, load=3, limit=3, calendar=workdays) == monday + dt.timedelta(
        days=1,
    )
    # Лимит 2 и 3 в работе: нужно дождаться двух — в среду.
    assert free_slot_date(ready, load=3, limit=2, calendar=workdays) == monday + dt.timedelta(
        days=2,
    )


def test_free_slot_skips_weekend(workdays, monday):
    friday = monday + dt.timedelta(days=4)

    assert free_slot_date([friday], load=1, limit=1, calendar=workdays) == monday + dt.timedelta(
        days=7,
    )


def test_free_slot_unknown_when_places_held_by_unpaid(workdays, monday):
    # В очереди один заказ, остальные места держат неоплаченные — даты нет.
    assert free_slot_date([monday], load=3, limit=1, calendar=workdays) is None


def test_free_slot_not_needed_below_limit(workdays, monday):
    assert free_slot_date([monday], load=1, limit=5, calendar=workdays) is None


def test_promised_date_adds_working_days(workdays, monday):
    friday = monday + dt.timedelta(days=4)
    intake = IntakeSettings(extra_lead_days=1)

    assert promised_date(friday, intake=intake, calendar=workdays) == monday + dt.timedelta(
        days=7,
    )
    assert promised_date(friday, intake=IntakeSettings(), calendar=workdays) == friday
    assert promised_date(None, intake=intake, calendar=workdays) is None


def test_pause_respects_resume_date(monday):
    paused = IntakeSettings(accepting_orders=False, resume_on=monday + dt.timedelta(days=3))

    assert paused.is_paused(monday) is True
    assert paused.is_paused(monday + dt.timedelta(days=3)) is False
    assert IntakeSettings(accepting_orders=False).is_paused(monday) is True
    assert IntakeSettings().is_paused(monday) is False


def test_email_check_catches_typos():
    assert is_valid_email("anna@mail.ru")
    assert is_valid_email(" anna.k@yandex.ru ")
    assert not is_valid_email("")
    assert not is_valid_email(None)
    assert not is_valid_email("anna@mail")
    assert not is_valid_email("anna mail@mail.ru")
    assert not is_valid_email("anna@@mail.ru")
    assert not is_valid_email("@mail.ru")


def _order(**fields) -> Order:
    order = Order(id=1, total_kopecks=170000, **fields)
    order.items = []
    order.addons = []
    return order


def test_unpaid_order_is_named_by_sum():
    assert _order(number=None).display_number == "на 1\u00a0700\u00a0₽"
    assert _order(number="EM-1025").display_number == "EM-1025"
    assert _order(number=None).admin_label == "б/н #1"


def test_only_expired_orders_can_be_revived():
    expired = _order(status=OrderStatus.CANCELLED, cancel_reason=CancelReason.EXPIRED)
    by_customer = _order(status=OrderStatus.CANCELLED, cancel_reason=CancelReason.CUSTOMER)
    already_paid = _order(
        status=OrderStatus.CANCELLED,
        cancel_reason=CancelReason.EXPIRED,
        paid_at=dt.datetime(2026, 9, 21, tzinfo=dt.UTC),
    )

    assert can_revive(expired) is True
    assert can_revive(by_customer) is False
    assert can_revive(already_paid) is False
