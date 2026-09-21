"""Редактируемые из админки настройки: тексты уведомлений, параметры, учётные записи."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.models.base import Base, IdMixin, TimestampMixin


class MessageTemplate(IdMixin, TimestampMixin, Base):
    """Шаблон уведомления клиенту.

    Текст — Jinja2 в песочнице. Неизвестный плейсхолдер не сохраняется:
    при сохранении в админке шаблон прогоняется тестовым рендером.
    """

    __tablename__ = "message_templates"

    key: Mapped[str] = mapped_column(String(64), unique=True)
    text: Mapped[str] = mapped_column(Text)
    is_enabled: Mapped[bool] = mapped_column(default=True, server_default="true")

    def __repr__(self) -> str:
        return f"<MessageTemplate {self.key}>"


class Setting(IdMixin, TimestampMixin, Base):
    """Пара ключ-значение. Значение разбирается Pydantic-схемой при чтении."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), unique=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")

    def __repr__(self) -> str:
        return f"<Setting {self.key}>"


class AdminUser(IdMixin, TimestampMixin, Base):
    """Учётная запись для входа в админку. Создаётся из командной строки."""

    __tablename__ = "admin_users"

    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(default=True, server_default="true")
    last_login_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        default=None,
    )

    def __repr__(self) -> str:
        return f"<AdminUser {self.username}>"
