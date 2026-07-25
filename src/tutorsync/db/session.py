"""Движок и фабрика сессий."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from tutorsync.config import Settings, get_settings


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    settings: Settings = get_settings()
    kwargs: dict[str, object] = {"echo": False, "future": True}
    if settings.is_postgres:
        # pool_pre_ping: соединения переживают ночь простоя, а Postgres их к утру
        # закрывает — без пинга первый запрос после паузы падает.
        kwargs |= {"pool_size": 5, "max_overflow": 5, "pool_pre_ping": True}
    return create_async_engine(settings.database_url, **kwargs)


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False, autoflush=False)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Транзакция целиком: коммит на выходе, откат на исключении.

    Внешние вызовы (Google, Telegram) внутрь этого блока не помещаются —
    они ставятся в outbox и выполняются после коммита.
    """
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    await get_engine().dispose()
