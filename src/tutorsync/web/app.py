"""HTTP-слой. Единственный процесс, смотрящий наружу через Cloudflare Tunnel.

Открыты два пути: проба /healthz и колбэк OAuth. Приём push-уведомлений Google
(/webhook/gcal/...) появится вместе с каналами events.watch — регистрировать
маршрут раньше нечем, обработчику нечего было бы делать.

Колбэк публичен по необходимости: на него редиректит браузер пользователя из
Google, и закрыть его аутентификацией нельзя. Защита — одноразовый подписанный
state, который выдаёт бот и только админу; см. tutorsync.gcal.oauth.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import sqlalchemy as sa
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

from tutorsync import __version__
from tutorsync.config import get_settings
from tutorsync.db.session import dispose_engine, get_sessionmaker, session_scope
from tutorsync.gcal.client import CalendarClient
from tutorsync.gcal.oauth import (
    InvalidStateError,
    exchange_code,
    store_credentials,
    verify_state,
)
from tutorsync.logging import get_logger, trace

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    log.info("web.started", version=__version__)
    yield
    await dispose_engine()
    log.info("web.stopped")


app = FastAPI(
    title="tutorsync", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None
)


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


def _page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    """Минимальная страница для человека с браузером.

    Шаблонизатор ради двух экранов не нужен, а JSON здесь неуместен: сюда
    приходит не программа, а человек, только что нажавший «Разрешить».
    """
    return HTMLResponse(
        status_code=status_code,
        content=(
            "<!doctype html><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{title}</title>"
            "<body style=\"font:16px/1.5 system-ui,sans-serif;max-width:34em;"
            'margin:15vh auto;padding:0 1.2em">'
            f"<h1 style='font-size:1.3em'>{title}</h1>{body}"
        ),
    )


@app.get("/oauth/callback")
async def oauth_callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> HTMLResponse:
    """Куда Google возвращает браузер после экрана согласия.

    Порядок проверок здесь не случаен: state проверяется до того, как код уходит
    в Google на обмен. Иначе чужой запрос с произвольным кодом заставлял бы
    сервис ходить наружу — бесплатный способ нагрузить нас чужими руками.
    """
    with trace(channel="oauth"):
        if error:
            log.warning("oauth.declined", error=error)
            return _page(
                "Доступ не выдан",
                f"<p>Google вернул: <code>{error}</code>.</p>"
                "<p>Запроси новую ссылку командой /connect_google и попробуй ещё раз.</p>",
                status_code=400,
            )

        if not code or not state:
            return _page(
                "Некорректный запрос",
                "<p>В адресе нет кода авторизации. Открой ссылку из бота целиком.</p>",
                status_code=400,
            )

        try:
            code_verifier = verify_state(state)
        except InvalidStateError as exc:
            log.warning("oauth.bad_state", error=str(exc))
            return _page(
                "Ссылка недействительна",
                f"<p>{exc}</p><p>Отправь боту /connect_google — он выдаст свежую.</p>",
                status_code=403,
            )

        try:
            creds = await exchange_code(code, code_verifier)
            email = await CalendarClient(creds).account_email()
            async with session_scope() as session:
                await store_credentials(session, creds, email)
        # Показать человеку причину, а не голый 500: он сейчас в браузере
        # и без объяснения не поймёт, что делать дальше.
        except Exception as exc:
            log.error("oauth.exchange_failed", error=str(exc))
            return _page(
                "Не удалось завершить авторизацию",
                f"<p><code>{type(exc).__name__}: {exc}</code></p>"
                "<p>Частая причина — расхождение redirect URI в Google Cloud Console "
                "с переменной GOOGLE_OAUTH_REDIRECT_URI.</p>",
                status_code=500,
            )

        log.info("oauth.connected", account=email)
        return _page(
            "Готово",
            f"<p>Аккаунт <b>{email}</b> подключён. Вкладку можно закрыть.</p>",
        )
