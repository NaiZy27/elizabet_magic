"""Вход в панель и выход из неё."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Form, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from core.models import AdminUser
from web.security import (
    CurrentAdmin,
    DbSession,
    client_ip,
    csrf_token,
    is_blocked,
    is_safe_next,
    login_user,
    logout_user,
    register_failure,
    reset_failures,
    verify_csrf,
    verify_password,
)
from web.templating import render

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin"])


@router.get("/login")
async def login_form(request: Request, next: str = ""):
    if request.session.get("admin_id"):
        return RedirectResponse("/admin/board", status_code=status.HTTP_303_SEE_OTHER)
    return render(
        request,
        "admin/login.html",
        {"csrf_token": csrf_token(request), "next": next if is_safe_next(next) else ""},
    )


@router.post("/login")
async def login(
    request: Request,
    session: DbSession,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    csrf: Annotated[str, Form(alias="csrf_token")] = "",
    next: Annotated[str, Form()] = "",
):
    verify_csrf(request, csrf)
    ip = client_ip(request)
    username = username.strip()

    if await is_blocked(ip, username):
        logger.warning("Вход заблокирован по числу попыток: %s", ip)
        return render(
            request,
            "admin/login.html",
            {
                "csrf_token": csrf_token(request),
                "error": "Слишком много попыток. Попробуйте через 15 минут.",
                "next": next,
            },
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    user = await session.scalar(select(AdminUser).where(AdminUser.username == username))
    password_hash = user.password_hash if user is not None else None

    # Пароль проверяем всегда, даже для несуществующего логина: иначе по времени
    # ответа можно перебрать существующие логины.
    if not verify_password(password, password_hash) or user is None or not user.is_active:
        await register_failure(ip, username)
        return render(
            request,
            "admin/login.html",
            {
                "csrf_token": csrf_token(request),
                "error": "Неверный логин или пароль",
                "next": next,
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    await reset_failures(ip, username)
    await login_user(request, user)
    target = next if is_safe_next(next) else "/admin/board"
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/logout")
async def logout(
    request: Request,
    admin: CurrentAdmin,
    csrf: Annotated[str, Form(alias="csrf_token")] = "",
):
    verify_csrf(request, csrf)
    logout_user(request)
    return RedirectResponse("/admin/login", status_code=status.HTTP_303_SEE_OTHER)
