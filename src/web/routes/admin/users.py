"""Пользователи панели: владелица заводит сборщиков и управляет доступом.

Пароли хранятся только bcrypt-хэшем. Себя владелица не может ни отключить,
ни разжаловать — иначе можно остаться без единого входа в панель.
"""

from __future__ import annotations

from typing import Annotated, NoReturn

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from core.db import folded
from core.enums import AdminRole
from core.models import AdminUser
from web.security import (
    MIN_PASSWORD_LENGTH,
    CurrentOwner,
    DbSession,
    csrf_token,
    hash_password,
    verify_csrf,
)
from web.templating import render

router = APIRouter(prefix="/users", tags=["admin"])


@router.get("")
async def users_page(request: Request, session: DbSession, admin: CurrentOwner):
    users = await session.scalars(select(AdminUser).order_by(AdminUser.id))
    return render(
        request,
        "admin/users.html",
        {
            "users": list(users),
            "roles": list(AdminRole),
            "min_password_length": MIN_PASSWORD_LENGTH,
            "admin": admin,
            "csrf_token": csrf_token(request),
        },
    )


@router.post("")
async def create_user(
    request: Request,
    session: DbSession,
    admin: CurrentOwner,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    role: Annotated[str, Form()] = AdminRole.ASSEMBLER.value,
    csrf: Annotated[str, Form(alias="csrf_token")] = "",
):
    verify_csrf(request, csrf)
    login = username.strip()
    if not login:
        _fail("Укажите логин")
    _check_password(password)
    exists = await session.scalar(
        select(func.count(AdminUser.id)).where(folded(AdminUser.username) == login.lower()),
    )
    if exists:
        _fail(f"Логин «{login}» уже занят")

    session.add(
        AdminUser(
            username=login,
            password_hash=hash_password(password),
            role=_parse_role(role),
            is_active=True,
        ),
    )
    await session.flush()
    return _back()


@router.post("/{user_id}")
async def update_user(
    request: Request,
    user_id: int,
    session: DbSession,
    admin: CurrentOwner,
    role: Annotated[str, Form()],
    is_active: Annotated[bool, Form()] = False,
    password: Annotated[str, Form()] = "",
    csrf: Annotated[str, Form(alias="csrf_token")] = "",
):
    """Роль, доступ и новый пароль. Пустое поле пароля — пароль не меняется."""
    verify_csrf(request, csrf)
    user = await session.get(AdminUser, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Пользователь не найден")

    new_role = _parse_role(role)
    if user.id == admin.id and (new_role != AdminRole.OWNER or not is_active):
        _fail("Себя нельзя отключить или лишить роли владелицы")

    user.role = new_role
    user.is_active = is_active
    if password:
        _check_password(password)
        user.password_hash = hash_password(password)
    await session.flush()
    return _back()


def _parse_role(value: str) -> AdminRole:
    try:
        return AdminRole(value)
    except ValueError:
        _fail("Неизвестная роль")


def _check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        _fail(f"Пароль должен быть не короче {MIN_PASSWORD_LENGTH} символов")


def _fail(message: str) -> NoReturn:
    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=message)


def _back() -> RedirectResponse:
    return RedirectResponse("/admin/users", status_code=status.HTTP_303_SEE_OTHER)
