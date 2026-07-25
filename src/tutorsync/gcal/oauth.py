"""Авторизация в Google и хранение токенов.

Проходится один раз руками: админ просит ссылку у бота, открывает её, соглашается
— и сервис получает refresh-токен, по которому дальше работает без человека.

Две вещи, определяющие устройство этого модуля.

**Колбэк открыт наружу.** ``/oauth/callback`` обязан быть публичным: на него
редиректит Google из браузера, и закрыть его аутентификацией нельзя. Значит любой,
кто знает адрес, может дойти до него со своим кодом авторизации — и, если бы мы
принимали всё подряд, подсунуть сервису чужой аккаунт, после чего расписание
поехало бы в чужой календарь. Отсюда параметр ``state``, подписанный HMAC на
SECRET_ENC_KEY: ссылку выдаёт только бот и только админу, живёт она десять минут.

**Refresh-токен выдаётся не всегда.** Google отдаёт его только при первом согласии,
а на повторных возвращает лишь access-токен. Поэтому ``prompt=consent`` стоит
всегда: без него переподключение аккаунта тихо сохранило бы запись без
refresh-токена, и сервис умер бы через час — ровно тогда, когда истечёт access.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import hmac
import time

import sqlalchemy as sa
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from sqlalchemy.ext.asyncio import AsyncSession

from tutorsync.config import Settings, get_settings
from tutorsync.crypto import decrypt, encrypt
from tutorsync.db.models import OAuthCredential
from tutorsync.logging import get_logger

log = get_logger(__name__)

#: Полный доступ к календарю. Уже нужен: сервис не только читает занятость,
#: но и создаёт, правит и удаляет события в трёх своих календарях.
#: calendar.events было бы достаточно для событий, но не даёт events.watch
#: на чужие календари, а без него не будет push-уведомлений.
SCOPES = ["https://www.googleapis.com/auth/calendar"]

#: Сколько живёт ссылка авторизации. Десять минут — с запасом на «открою с
#: телефона», но мало для того, чтобы ссылка осела в истории браузера надолго.
STATE_TTL_SEC = 600


class OAuthNotConfiguredError(RuntimeError):
    """Не заданы GOOGLE_CLIENT_ID/SECRET — авторизовываться нечем."""


class NoCredentialsError(RuntimeError):
    """Авторизация ещё не пройдена: в базе нет ни одного refresh-токена."""


class InvalidStateError(RuntimeError):
    """Колбэк пришёл без валидного state — либо просрочен, либо чужой."""


# ==============================================================================
#  Одноразовый state
# ==============================================================================


def _sign(expires_at: int) -> str:
    key = get_settings().secret_enc_key.encode()
    return hmac.new(key, str(expires_at).encode(), hashlib.sha256).hexdigest()[:32]


def make_state(now: float | None = None) -> str:
    """Подписанная метка «эту ссылку выдал сервис, и вот когда она протухнет»."""
    expires_at = int((now if now is not None else time.time()) + STATE_TTL_SEC)
    return f"{expires_at}.{_sign(expires_at)}"


def verify_state(state: str, now: float | None = None) -> None:
    """Бросает InvalidStateError, если state подделан или просрочен."""
    expires_at_raw, _, signature = state.partition(".")
    if not signature:
        raise InvalidStateError("state без подписи")
    try:
        expires_at = int(expires_at_raw)
    except ValueError as exc:
        raise InvalidStateError("state с нечисловым сроком годности") from exc

    # compare_digest, а не ==: обычное сравнение строк выходит на первом
    # несовпавшем символе и по времени ответа позволяет подбирать подпись.
    if not hmac.compare_digest(signature, _sign(expires_at)):
        raise InvalidStateError("подпись state не сходится")
    if (now if now is not None else time.time()) > expires_at:
        raise InvalidStateError("ссылка авторизации просрочена, запроси новую")


# ==============================================================================
#  Поток авторизации
# ==============================================================================


def _client_config(settings: Settings) -> dict[str, dict[str, object]]:
    if not settings.google_client_id or not settings.google_client_secret:
        raise OAuthNotConfiguredError(
            "Не заданы GOOGLE_CLIENT_ID и GOOGLE_CLIENT_SECRET — "
            "авторизацию в Google пройти нечем."
        )
    return {
        "web": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [settings.google_oauth_redirect_uri],
        }
    }


def _flow(settings: Settings) -> Flow:
    flow = Flow.from_client_config(_client_config(settings), scopes=SCOPES)
    flow.redirect_uri = settings.google_oauth_redirect_uri
    return flow


def build_auth_url(state: str) -> str:
    settings = get_settings()
    url, _ = _flow(settings).authorization_url(
        # offline — иначе refresh-токена не будет вовсе и сервис проживёт час.
        access_type="offline",
        # consent — иначе refresh-токен придёт только при самой первой
        # авторизации, а при переподключении молча не придёт.
        prompt="consent",
        include_granted_scopes="true",
        state=state,
    )
    return str(url)


async def exchange_code(code: str) -> Credentials:
    """Меняет код авторизации на токены. Сетевой вызов — уводим из event loop."""
    settings = get_settings()
    flow = _flow(settings)

    def _fetch() -> Credentials:
        flow.fetch_token(code=code)
        return flow.credentials

    return await asyncio.to_thread(_fetch)


# ==============================================================================
#  Хранение
# ==============================================================================


async def store_credentials(session: AsyncSession, creds: Credentials, email: str) -> None:
    """Кладёт токены в базу, шифруя refresh.

    Refresh-токен не перезаписывается пустым значением: Google при повторной
    авторизации может его не прислать, и затерев сохранённый, мы бы своими
    руками сломали работающую интеграцию.
    """
    if not creds.refresh_token:
        raise RuntimeError(
            "Google не вернул refresh-токен. Обычная причина — авторизация без "
            "prompt=consent при уже выданном согласии. Отзови доступ приложения "
            "в https://myaccount.google.com/permissions и повтори."
        )

    row = (
        await session.execute(
            sa.select(OAuthCredential).where(OAuthCredential.account_email == email)
        )
    ).scalar_one_or_none()

    expiry = creds.expiry
    if expiry is not None and expiry.tzinfo is None:
        # google-auth отдаёт expiry naive в UTC, а UtcDateTime naive не принимает.
        expiry = expiry.replace(tzinfo=dt.UTC)

    if row is None:
        row = OAuthCredential(account_email=email)
        session.add(row)

    row.refresh_token_enc = encrypt(creds.refresh_token)
    row.access_token_enc = encrypt(creds.token) if creds.token else None
    row.token_expiry = expiry
    row.scopes = " ".join(creds.scopes or SCOPES)
    log.info("gcal.oauth.stored", account=email, scopes=row.scopes)


async def load_credentials(session: AsyncSession) -> Credentials:
    """Достаёт токены из базы и при необходимости обновляет access.

    Обновлённый access-токен сохраняется обратно: иначе каждый вызов начинался бы
    с похода в Google за новым, а это лишняя точка отказа на ровном месте.
    """
    row = (
        await session.execute(sa.select(OAuthCredential).order_by(OAuthCredential.id).limit(1))
    ).scalar_one_or_none()
    if row is None:
        raise NoCredentialsError(
            "Авторизация Google не пройдена. Отправь боту /connect_google и открой ссылку."
        )

    settings = get_settings()
    creds = Credentials(
        token=decrypt(row.access_token_enc) if row.access_token_enc else None,
        refresh_token=decrypt(row.refresh_token_enc),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=row.scopes.split() or SCOPES,
        expiry=row.token_expiry.replace(tzinfo=None) if row.token_expiry else None,
    )

    if not creds.valid:
        await asyncio.to_thread(creds.refresh, Request())
        row.access_token_enc = encrypt(creds.token)
        row.token_expiry = (
            creds.expiry.replace(tzinfo=dt.UTC) if creds.expiry is not None else None
        )
        log.debug("gcal.oauth.refreshed", account=row.account_email)

    return creds


async def has_credentials(session: AsyncSession) -> bool:
    count = await session.scalar(sa.select(sa.func.count()).select_from(OAuthCredential))
    return bool(count)
