"""Чтение и запись настроек.

Настройки лежат в базе, чтобы владелица меняла их из админки. Битое или устаревшее
значение не должно ронять бота, поэтому при ошибке разбора возвращаются умолчания,
а в лог уходит предупреждение.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import TypeVar

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Setting
from core.schemas.settings import (
    SETTING_SCHEMAS,
    BotTextsSettings,
    ContactsSettings,
    FaqSettings,
    IntakeSettings,
    OrderRulesSettings,
    SettingKey,
    SettingsModel,
    WorkingCalendarSettings,
)
from core.workdays import WorkingCalendar

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=SettingsModel)


async def read_setting(session: AsyncSession, key: SettingKey, schema: type[T]) -> T:
    """Значение настройки, разобранное схемой. Нет записи или битая — умолчания."""
    raw = await session.scalar(select(Setting.value).where(Setting.key == key.value))
    if not raw:
        return schema()
    try:
        return schema.model_validate(raw)
    except PydanticValidationError:
        logger.warning("Настройка %s хранится в неверном формате, взяты умолчания", key.value)
        return schema()


async def write_setting(session: AsyncSession, key: SettingKey, value: SettingsModel) -> None:
    """Сохранить настройку. Схема проверяется до записи — в базу не попадёт мусор."""
    expected = SETTING_SCHEMAS[key]
    if not isinstance(value, expected):
        raise TypeError(f"Настройка {key.value} ожидает {expected.__name__}")

    payload = value.model_dump(mode="json")
    statement = (
        insert(Setting)
        .values(key=key.value, value=payload)
        .on_conflict_do_update(index_elements=[Setting.key], set_={"value": payload})
    )
    await session.execute(statement)


async def get_working_calendar(session: AsyncSession) -> WorkingCalendar:
    """Рабочий календарь для расчёта дат готовности."""
    settings = await read_setting(session, SettingKey.WORKING_CALENDAR, WorkingCalendarSettings)
    return settings.to_calendar()


async def get_order_rules(session: AsyncSession) -> OrderRulesSettings:
    return await read_setting(session, SettingKey.ORDER_RULES, OrderRulesSettings)


async def get_intake(session: AsyncSession) -> IntakeSettings:
    return await read_setting(session, SettingKey.INTAKE, IntakeSettings)


async def get_unpaid_ttl(session: AsyncSession) -> dt.timedelta:
    """Сколько живёт неоплаченный заказ."""
    rules = await get_order_rules(session)
    return dt.timedelta(minutes=rules.unpaid_ttl_minutes)


async def get_contacts(session: AsyncSession) -> ContactsSettings:
    return await read_setting(session, SettingKey.CONTACTS, ContactsSettings)


async def get_faq(session: AsyncSession) -> FaqSettings:
    return await read_setting(session, SettingKey.FAQ, FaqSettings)


async def get_bot_texts(session: AsyncSession) -> BotTextsSettings:
    return await read_setting(session, SettingKey.BOT_TEXTS, BotTextsSettings)
