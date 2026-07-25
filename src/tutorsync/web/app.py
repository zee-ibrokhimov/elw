"""HTTP-слой. Единственный процесс, смотрящий наружу через Cloudflare Tunnel.

Сейчас открыт только /healthz. Приём push-уведомлений Google (/webhook/gcal/...)
и колбэк OAuth появятся на этапе 2 — регистрировать маршруты заранее нечем:
каналы events.watch ещё не создаются, и обработчику нечего было бы делать.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from tutorsync import __version__
from tutorsync.config import get_settings
from tutorsync.db.session import dispose_engine, get_sessionmaker
from tutorsync.logging import get_logger

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    log.info("web.started", version=__version__)
    yield
    await dispose_engine()
    log.info("web.stopped")


app = FastAPI(title="tutorsync", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Проба для мониторинга: живость процесса плюс доступность базы.

    Без проверки базы healthz отвечал бы 200 у процесса, который не может
    обслужить ни одного запроса, — и мониторинг молчал бы при упавшем Postgres.
    """
    settings = get_settings()
    try:
        async with get_sessionmaker()() as session:
            await session.execute(sa.text("SELECT 1"))
    except Exception as exc:
        log.error("healthz.db_unavailable", error=str(exc))
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "database": "unavailable", "version": __version__},
        )

    return JSONResponse(
        content={
            "status": "ok",
            "database": "ok",
            "version": __version__,
            "preply_gcal_linked": settings.preply_gcal_linked,
        }
    )
