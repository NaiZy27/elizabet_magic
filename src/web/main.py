"""Веб-процесс: админка, страница оплаты и вебхуки.

Наружу смотрит только страница оплаты; админка живёт на отдельном поддомене,
не индексируется и доступна лишь по логину.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from core.cache import close_redis
from core.config import get_settings
from core.db import dispose_engine
from web.routes import pay
from web.routes.admin import router as admin_router
from web.security import NotAuthenticated, login_redirect
from web.templating import STATIC_DIR, render
from worker.broker import ensure_started, shutdown

logger = logging.getLogger(__name__)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Заголовки, которые должны стоять на каждой странице панели."""

    def __init__(self, app, *, https_only: bool) -> None:
        super().__init__(app)
        self.https_only = https_only

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; form-action 'self'; frame-ancestors 'none'",
        )
        if request.url.path.startswith("/admin"):
            # Страницы с заказами не должны оседать в кэше браузера.
            response.headers.setdefault("Cache-Control", "no-store")
        if self.https_only:
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        return response


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await ensure_started()
    try:
        yield
    finally:
        await shutdown()
        await close_redis()
        await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Elizabet Magic",
        lifespan=lifespan,
        # Автодокументация наружу не нужна.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    secret = settings.admin.session_secret.get_secret_value()
    if not secret:
        if settings.environment == "production":
            raise RuntimeError("ADMIN_SESSION_SECRET обязателен в боевом окружении")
        secret = secrets.token_urlsafe(48)
        logger.warning("ADMIN_SESSION_SECRET не задан: сгенерирован временный ключ на запуск")

    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        session_cookie="em_admin",
        max_age=settings.admin.session_ttl_hours * 3600,
        same_site="strict",
        https_only=settings.admin.https_only,
    )
    app.add_middleware(SecurityHeadersMiddleware, https_only=settings.admin.https_only)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(admin_router)
    app.include_router(pay.router)

    _register_error_handlers(app)

    @app.get("/robots.txt", include_in_schema=False)
    async def robots() -> PlainTextResponse:
        return PlainTextResponse("User-agent: *\nDisallow: /\n")

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/admin/board")

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> PlainTextResponse:
        return PlainTextResponse("ok")

    return app


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(NotAuthenticated)
    async def _not_authenticated(request: Request, error: NotAuthenticated) -> RedirectResponse:
        return login_redirect(error.next_url)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, error: StarletteHTTPException) -> Response:
        if request.url.path.startswith("/api/") or not _wants_html(request):
            return Response(
                content=str(error.detail),
                status_code=error.status_code,
                media_type="text/plain; charset=utf-8",
            )
        return render(
            request,
            "errors/error.html",
            {
                "code": error.status_code,
                "title": _error_title(error.status_code),
                "message": error.detail,
            },
            status_code=error.status_code,
        )


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


def _error_title(code: int) -> str:
    return {
        403: "Доступ закрыт",
        404: "Страница не найдена",
        422: "Данные не подошли",
        500: "Что-то пошло не так",
    }.get(code, "Ошибка")
