"""Вход в админку, сессии, CSRF и защита от перебора паролей.

Панель не индексируется и ссылок на неё нигде нет, но это не защита. Защита —
длинный пароль, короткая сессия, ограничение попыток входа и CSRF-токен в каждой форме.
"""

from __future__ import annotations

import hmac
import logging
import secrets
from typing import Annotated

import bcrypt
from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.cache import limits_redis
from core.clock import now_utc
from core.config import get_settings
from core.db import get_db_session
from core.models import AdminUser

logger = logging.getLogger(__name__)

SESSION_USER_ID = "admin_id"
SESSION_USERNAME = "admin_username"
SESSION_CSRF = "csrf_token"

#: Хэш-пустышка: сравниваем с ним пароль для несуществующего логина,
#: чтобы время ответа не выдавало, какие логины существуют.
_DUMMY_HASH = bcrypt.hashpw(b"dummy-password-for-timing", bcrypt.gensalt())

MIN_PASSWORD_LENGTH = 12


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str | None) -> bool:
    """Проверить пароль. Для несуществующего логина всё равно считаем хэш."""
    candidate = password.encode("utf-8")
    if password_hash is None:
        bcrypt.checkpw(candidate, _DUMMY_HASH)
        return False
    try:
        return bcrypt.checkpw(candidate, password_hash.encode("utf-8"))
    except ValueError:
        logger.warning("В базе испорченный хэш пароля")
        return False


# --- ограничение попыток входа ---


def _attempts_key(ip: str, username: str) -> str:
    return f"login:{ip}:{username.lower()}"


async def is_blocked(ip: str, username: str) -> bool:
    settings = get_settings()
    value = await limits_redis().get(_attempts_key(ip, username))
    return value is not None and int(value) >= settings.admin.login_max_attempts


async def register_failure(ip: str, username: str) -> None:
    """Счётчик неудач живёт ровно столько, сколько длится блокировка."""
    settings = get_settings()
    key = _attempts_key(ip, username)
    redis = limits_redis()
    attempts = await redis.incr(key)
    if attempts == 1:
        await redis.expire(key, settings.admin.login_block_minutes * 60)


async def reset_failures(ip: str, username: str) -> None:
    await limits_redis().delete(_attempts_key(ip, username))


def client_ip(request: Request) -> str:
    """Адрес клиента. За nginx его подставляет uvicorn с --proxy-headers."""
    return request.client.host if request.client else "unknown"


# --- сессия ---


async def login_user(request: Request, user: AdminUser) -> None:
    """Открыть сессию. Идентификатор пересоздаётся, старые данные стираются."""
    request.session.clear()
    request.session[SESSION_USER_ID] = user.id
    request.session[SESSION_USERNAME] = user.username
    request.session[SESSION_CSRF] = secrets.token_urlsafe(32)
    user.last_login_at = now_utc()


def logout_user(request: Request) -> None:
    request.session.clear()


def csrf_token(request: Request) -> str:
    """Токен для форм. Создаётся вместе с сессией и живёт столько же."""
    token = request.session.get(SESSION_CSRF)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[SESSION_CSRF] = token
    return token


def verify_csrf(request: Request, submitted: str | None) -> None:
    expected = request.session.get(SESSION_CSRF)
    if not expected or not submitted or not hmac.compare_digest(expected, submitted):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Форма устарела. Обновите страницу и попробуйте ещё раз.",
        )


class RequireLogin:
    """Зависимость: пускает только с открытой сессией и нужной ролью.

    HTML-страницы уводят на форму входа, запросы к /api отвечают 401 —
    чтобы доска показала внятную ошибку, а не редирект в JSON. Вошедший, но без
    нужной роли, получает 403.
    """

    def __init__(self, *, api: bool = False, owner_only: bool = False) -> None:
        self.api = api
        self.owner_only = owner_only

    async def __call__(
        self,
        request: Request,
        session: Annotated[AsyncSession, Depends(get_db_session)],
    ) -> AdminUser:
        user_id = request.session.get(SESSION_USER_ID)
        user = None
        if user_id is not None:
            user = await session.scalar(
                select(AdminUser).where(AdminUser.id == user_id, AdminUser.is_active.is_(True)),
            )

        if user is None:
            request.session.clear()
            if self.api:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Сессия закончилась, войдите заново",
                )
            raise NotAuthenticated(next_url=request.url.path)

        if self.owner_only and not user.is_owner:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Этот раздел доступен только владелице",
            )
        return user


class NotAuthenticated(Exception):
    """Нет сессии: обработчик уведёт на страницу входа."""

    def __init__(self, next_url: str) -> None:
        super().__init__("Требуется вход")
        self.next_url = next_url


def login_redirect(next_url: str | None) -> RedirectResponse:
    """Увести на вход, запомнив, куда человек шёл."""
    target = "/admin/login"
    if next_url and is_safe_next(next_url):
        target = f"{target}?next={next_url}"
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)


def is_safe_next(value: str) -> bool:
    """Возвращаем только на свои страницы: «//example.com» — чужой сайт."""
    return value.startswith("/") and not value.startswith("//")


#: Готовые зависимости. CurrentAdmin — любой вошедший (владелица или сборщик),
#: CurrentOwner — только владелица: деньги, каталог, клиенты, настройки, пользователи.
require_login = RequireLogin()
require_login_api = RequireLogin(api=True)
require_owner = RequireLogin(owner_only=True)
CurrentAdmin = Annotated[AdminUser, Depends(require_login)]
CurrentAdminApi = Annotated[AdminUser, Depends(require_login_api)]
CurrentOwner = Annotated[AdminUser, Depends(require_owner)]
DbSession = Annotated[AsyncSession, Depends(get_db_session)]
