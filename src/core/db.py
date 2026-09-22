"""Подключение к PostgreSQL.

Движок создаётся лениво и один раз на процесс: у bot, web и worker он свой.
Сессия живёт коротко — на одну операцию или один запрос.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy import func
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from core.config import get_settings

logger = logging.getLogger(__name__)


def folded(column):
    """Колонка в нижнем регистре для поиска без учёта регистра.

    Обычный lower() берёт правила из локали базы, а у базы с локалью C он не трогает
    кириллицу: «анна» не находит «Анна». Сортировка ICU работает одинаково при любой
    локали — и на машине разработчика, и в контейнере.
    """
    return func.lower(column.collate("und-x-icu"))


#: Ключ в session.info, под которым копятся действия «после успешного коммита».
_AFTER_COMMIT = "after_commit"


def after_commit(session: AsyncSession, action: Callable[[], Awaitable[None]]) -> None:
    """Выполнить действие только если транзакция закоммитится.

    Так уведомления клиенту не уходят по откатившейся транзакции: сообщение
    «заказ готов» нельзя забрать обратно, а запись в базе — можно.
    """
    session.info.setdefault(_AFTER_COMMIT, []).append(action)


async def run_after_commit(session: AsyncSession) -> None:
    """Выполнить накопленные действия. Их ошибки не отменяют уже сохранённые данные."""
    actions: list[Callable[[], Awaitable[None]]] = session.info.pop(_AFTER_COMMIT, [])
    for action in actions:
        try:
            await action()
        except Exception:
            logger.exception("Не удалось выполнить действие после коммита")


@lru_cache
def get_engine() -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        # Соединение могло протухнуть, пока ночью не было заказов.
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_recycle=1800,
        echo=False,
    )


@lru_cache
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=get_engine(),
        expire_on_commit=False,  # объекты нужны после commit — в уведомлениях и ответах
        autoflush=False,
    )


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Сессия с транзакцией: commit при успехе, rollback при исключении.

    Основной способ работы с базой в боте и фоновых задачах.
    """
    async with get_session_factory()() as session:
        try:
            yield session
        except Exception:
            session.info.pop(_AFTER_COMMIT, None)
            await session.rollback()
            raise
        else:
            await session.commit()
            await run_after_commit(session)


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """Зависимость FastAPI. Роут получает сессию, коммит — в конце успешного запроса."""
    async with session_scope() as session:
        yield session


async def dispose_engine() -> None:
    """Закрыть пул соединений при остановке процесса."""
    if get_engine.cache_info().currsize:
        await get_engine().dispose()
        get_engine.cache_clear()
        get_session_factory.cache_clear()
