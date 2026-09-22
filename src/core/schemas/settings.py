"""Значения из таблицы settings.

В базе это JSONB, а здесь — схемы, которыми значение разбирается при чтении.
Если в базе лежит мусор или старый формат, читатель получит значения по умолчанию,
а не исключение посреди оформления заказа.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.workdays import DEFAULT_WORKING_WEEKDAYS, WorkingCalendar


class SettingKey(StrEnum):
    """Ключи настроек. Каждому соответствует своя схема ниже."""

    WORKING_CALENDAR = "working_calendar"
    ORDER_RULES = "order_rules"
    INTAKE = "intake"
    CONTACTS = "contacts"
    FAQ = "faq"
    BOT_TEXTS = "bot_texts"


class SettingsModel(BaseModel):
    """Общая база: лишние ключи из базы игнорируем, а не падаем на них."""

    model_config = ConfigDict(extra="ignore")


class WorkingCalendarSettings(SettingsModel):
    """Когда мастерская собирает боксы. Понедельник — 0."""

    weekdays: list[int] = Field(default_factory=lambda: sorted(DEFAULT_WORKING_WEEKDAYS))
    #: Отпуск и праздники.
    holidays: list[dt.date] = Field(default_factory=list)

    @field_validator("weekdays")
    @classmethod
    def _check_weekdays(cls, value: list[int]) -> list[int]:
        if any(day < 0 or day > 6 for day in value):
            raise ValueError("День недели задаётся числом от 0 (понедельник) до 6 (воскресенье)")
        if not value:
            raise ValueError("Отметьте хотя бы один рабочий день недели")
        return sorted(set(value))

    def to_calendar(self) -> WorkingCalendar:
        return WorkingCalendar(
            weekdays=frozenset(self.weekdays),
            holidays=frozenset(self.holidays),
        )


class OrderRulesSettings(SettingsModel):
    """Правила оформления."""

    #: Через сколько минут неоплаченный заказ отменяется автоматически.
    unpaid_ttl_minutes: int = Field(default=60, gt=0, le=7 * 24 * 60)
    #: Сколько дней хранить заброшенные черновики.
    draft_ttl_days: int = Field(default=30, gt=0)
    #: Сколько боксов можно положить в один заказ.
    max_boxes_per_order: int = Field(default=10, ge=1, le=50)


class IntakeSettings(SettingsModel):
    """Приём заказов: пауза, лимит очереди и запас по срокам."""

    #: Выключатель приёма — например, на время отпуска.
    accepting_orders: bool = True
    #: Что сказать клиенту, пока приём закрыт.
    pause_message: str = (
        "Сейчас мы не принимаем новые заказы — скоро вернёмся 💗\n"
        "Следите за новостями в наших соцсетях."
    )
    #: С этого дня приём откроется сам. Пусто — откроется, только когда включат вручную.
    resume_on: dt.date | None = None
    #: Сколько заказов одновременно может быть в работе (оплаченные в очереди и на
    #: сборке плюс ожидающие оплаты). Пусто — без ограничения.
    queue_limit: int | None = Field(default=None, ge=1, le=1000)
    #: Сколько рабочих дней прибавить к сроку, который называем клиенту, — запас,
    #: когда владелица не успевает или хочет разгрузить неделю.
    extra_lead_days: int = Field(default=0, ge=0, le=60)

    def is_paused(self, today: dt.date) -> bool:
        """Закрыт ли приём сегодня с учётом даты автооткрытия."""
        if self.accepting_orders:
            return False
        return self.resume_on is None or today < self.resume_on


class ContactsSettings(SettingsModel):
    """Как с нами связаться — показывается в боте."""

    telegram: str | None = None
    phone: str | None = None
    email: str | None = None
    instagram: str | None = None
    tiktok: str | None = None


class FaqItem(SettingsModel):
    question: str
    answer: str


class FaqSettings(SettingsModel):
    items: list[FaqItem] = Field(default_factory=list)


class BotTextsSettings(SettingsModel):
    """Тексты бота, которые владелица правит без программиста."""

    greeting: str = (
        "Привет! Это мастерская подарочных боксов Elizabet Magic 💗\n"
        "Соберём для вас бокс с ложечками — выбирайте, что нравится."
    )
    wishes_disclaimer: str = (
        "Мы обязательно учитываем ваши пожелания, но некоторые элементы могут "
        "отсутствовать в наличии, поэтому возможны замены на аналогичные."
    )
    consent_text: str = (
        "Оформляя заказ, вы соглашаетесь на обработку персональных данных: "
        "имя, телефон и адрес пункта выдачи нужны для доставки."
    )
    consent_url: str | None = None


#: Какой схемой разбирать значение каждого ключа.
SETTING_SCHEMAS: dict[SettingKey, type[SettingsModel]] = {
    SettingKey.WORKING_CALENDAR: WorkingCalendarSettings,
    SettingKey.ORDER_RULES: OrderRulesSettings,
    SettingKey.INTAKE: IntakeSettings,
    SettingKey.CONTACTS: ContactsSettings,
    SettingKey.FAQ: FaqSettings,
    SettingKey.BOT_TEXTS: BotTextsSettings,
}
