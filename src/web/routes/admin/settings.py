"""Настройки: тексты уведомлений, рабочий календарь, контакты и вопросы."""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from core.enums import MessageTemplateKey
from core.errors import ValidationError
from core.labels import MESSAGE_TEMPLATE_LABELS
from core.models import MessageTemplate
from core.schemas.settings import (
    BotTextsSettings,
    ContactsSettings,
    FaqItem,
    FaqSettings,
    IntakeSettings,
    OrderRulesSettings,
    SettingKey,
    WorkingCalendarSettings,
)
from core.services import intake as intake_service
from core.services import notifications
from core.services import settings as settings_service
from web.security import CurrentOwner, DbSession, csrf_token, verify_csrf
from web.templating import render

router = APIRouter(prefix="/settings", tags=["admin"])

WEEKDAY_NAMES = [
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье",
]


@router.get("")
async def settings_page(request: Request, session: DbSession, admin: CurrentOwner):
    stored = {template.key: template for template in await session.scalars(select(MessageTemplate))}
    templates = []
    for key in MessageTemplateKey:
        title, when = MESSAGE_TEMPLATE_LABELS[key]
        template = stored.get(key.value)
        templates.append(
            {
                "key": key.value,
                "title": title,
                "when": when,
                "text": template.text if template else notifications.DEFAULT_TEMPLATES[key],
                "is_enabled": template.is_enabled if template else True,
            },
        )

    calendar = await settings_service.read_setting(
        session,
        SettingKey.WORKING_CALENDAR,
        WorkingCalendarSettings,
    )

    return render(
        request,
        "admin/settings.html",
        {
            "templates": templates,
            "placeholders": notifications.PLACEHOLDERS,
            "calendar": calendar,
            "weekday_names": list(enumerate(WEEKDAY_NAMES)),
            "rules": await settings_service.get_order_rules(session),
            "intake": await settings_service.get_intake(session),
            "intake_state": await intake_service.check(session),
            "intake_load": await intake_service.current_load(session),
            "contacts": await settings_service.get_contacts(session),
            "faq": await settings_service.get_faq(session),
            "bot_texts": await settings_service.get_bot_texts(session),
            "admin": admin,
            "csrf_token": csrf_token(request),
        },
    )


@router.post("/templates/{key}")
async def save_template(
    request: Request,
    key: str,
    session: DbSession,
    admin: CurrentOwner,
    text: Annotated[str, Form()],
    is_enabled: Annotated[bool, Form()] = False,
    csrf: Annotated[str, Form(alias="csrf_token")] = "",
):
    """Сохранить текст уведомления. Шаблон с неизвестной вставкой не сохраняется."""
    verify_csrf(request, csrf)
    try:
        template_key = MessageTemplateKey(key)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Такого сообщения нет",
        ) from None

    try:
        notifications.validate_template(text)
    except ValidationError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=error.message,
        ) from error

    template = await session.scalar(
        select(MessageTemplate).where(MessageTemplate.key == template_key.value),
    )
    if template is None:
        template = MessageTemplate(key=template_key.value, text=text)
        session.add(template)
    template.text = text
    template.is_enabled = is_enabled

    await session.flush()
    return _back()


@router.post("/calendar")
async def save_calendar(
    request: Request,
    session: DbSession,
    admin: CurrentOwner,
    weekdays: Annotated[list[int], Form()] = [],  # noqa: B006 — так FastAPI читает чекбоксы
    holidays: Annotated[str, Form()] = "",
    csrf: Annotated[str, Form(alias="csrf_token")] = "",
):
    """Рабочие дни и выходные даты — от них зависят все даты готовности."""
    verify_csrf(request, csrf)
    parsed: list[dt.date] = []
    for line in holidays.replace(",", "\n").split("\n"):
        value = line.strip()
        if not value:
            continue
        try:
            parsed.append(dt.date.fromisoformat(value))
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Дата «{value}» указана неверно. Формат: 2026-01-07",
            ) from None

    if not weekdays:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Отметьте хотя бы один рабочий день недели",
        )

    await settings_service.write_setting(
        session,
        SettingKey.WORKING_CALENDAR,
        WorkingCalendarSettings(weekdays=weekdays, holidays=parsed),
    )
    return _back()


@router.post("/rules")
async def save_rules(
    request: Request,
    session: DbSession,
    admin: CurrentOwner,
    unpaid_ttl_minutes: Annotated[int, Form(ge=5, le=10080)],
    draft_ttl_days: Annotated[int, Form(ge=1, le=365)],
    max_boxes_per_order: Annotated[int, Form(ge=1, le=50)] = 10,
    csrf: Annotated[str, Form(alias="csrf_token")] = "",
):
    verify_csrf(request, csrf)
    await settings_service.write_setting(
        session,
        SettingKey.ORDER_RULES,
        OrderRulesSettings(
            unpaid_ttl_minutes=unpaid_ttl_minutes,
            draft_ttl_days=draft_ttl_days,
            max_boxes_per_order=max_boxes_per_order,
        ),
    )
    return _back()


@router.post("/intake")
async def save_intake(
    request: Request,
    session: DbSession,
    admin: CurrentOwner,
    pause_message: Annotated[str, Form()],
    accepting_orders: Annotated[bool, Form()] = False,
    resume_on: Annotated[str, Form()] = "",
    queue_limit: Annotated[str, Form()] = "",
    extra_lead_days: Annotated[int, Form(ge=0, le=60)] = 0,
    csrf: Annotated[str, Form(alias="csrf_token")] = "",
):
    """Приём заказов: пауза, лимит одновременно в работе и запас по срокам."""
    verify_csrf(request, csrf)

    resume_date: dt.date | None = None
    if resume_on.strip():
        try:
            resume_date = dt.date.fromisoformat(resume_on.strip())
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Дата открытия приёма указана неверно",
            ) from None

    limit: int | None = None
    if queue_limit.strip():
        if not queue_limit.strip().isdigit() or int(queue_limit) < 1:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Лимит — целое число больше нуля, или оставьте поле пустым",
            )
        limit = int(queue_limit)

    if not pause_message.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Напишите, что показать клиентам, пока приём закрыт",
        )

    await settings_service.write_setting(
        session,
        SettingKey.INTAKE,
        IntakeSettings(
            accepting_orders=accepting_orders,
            pause_message=pause_message.strip(),
            resume_on=resume_date,
            queue_limit=limit,
            extra_lead_days=extra_lead_days,
        ),
    )
    return _back()


@router.post("/contacts")
async def save_contacts(
    request: Request,
    session: DbSession,
    admin: CurrentOwner,
    telegram: Annotated[str, Form()] = "",
    phone: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    instagram: Annotated[str, Form()] = "",
    tiktok: Annotated[str, Form()] = "",
    csrf: Annotated[str, Form(alias="csrf_token")] = "",
):
    verify_csrf(request, csrf)
    await settings_service.write_setting(
        session,
        SettingKey.CONTACTS,
        ContactsSettings(
            telegram=telegram.strip() or None,
            phone=phone.strip() or None,
            email=email.strip() or None,
            instagram=instagram.strip() or None,
            tiktok=tiktok.strip() or None,
        ),
    )
    return _back()


@router.post("/faq")
async def save_faq(
    request: Request,
    session: DbSession,
    admin: CurrentOwner,
    questions: Annotated[list[str], Form()] = [],  # noqa: B006 — списки полей формы
    answers: Annotated[list[str], Form()] = [],  # noqa: B006
    csrf: Annotated[str, Form(alias="csrf_token")] = "",
):
    """Вопросы и ответы для раздела «Частые вопросы» в боте."""
    verify_csrf(request, csrf)
    items = [
        FaqItem(question=question.strip(), answer=answer.strip())
        for question, answer in zip(questions, answers, strict=False)
        if question.strip() and answer.strip()
    ]
    await settings_service.write_setting(session, SettingKey.FAQ, FaqSettings(items=items))
    return _back()


@router.post("/bot-texts")
async def save_bot_texts(
    request: Request,
    session: DbSession,
    admin: CurrentOwner,
    greeting: Annotated[str, Form()],
    wishes_disclaimer: Annotated[str, Form()],
    consent_text: Annotated[str, Form()],
    consent_url: Annotated[str, Form()] = "",
    csrf: Annotated[str, Form(alias="csrf_token")] = "",
):
    verify_csrf(request, csrf)
    await settings_service.write_setting(
        session,
        SettingKey.BOT_TEXTS,
        BotTextsSettings(
            greeting=greeting.strip(),
            wishes_disclaimer=wishes_disclaimer.strip(),
            consent_text=consent_text.strip(),
            consent_url=consent_url.strip() or None,
        ),
    )
    return _back()


def _back() -> RedirectResponse:
    return RedirectResponse("/admin/settings", status_code=status.HTTP_303_SEE_OTHER)
