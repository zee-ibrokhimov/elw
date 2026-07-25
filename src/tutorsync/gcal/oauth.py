"""Авторизация в Google и хранение токенов.

Проходится один раз руками: админ просит ссылку у бота, открывает её, соглашается
— и сервис получает refresh-токен, по которому дальше работает без человека.

Две вещи, определяющие устройство этого модуля.

**Колбэк открыт наружу.** ``/oauth/callback`` обязан быть публичным: на него
редиректит Google из браузера, и закрыть его аутентификацией нельзя. Значит любой,
кто знает адрес, может дойти до него со своим кодом авторизации — и, если бы мы
принимали всё подряд, подсунуть сервису чужой аккаунт, после чего расписание
поехало бы в чужой календарь. Отсюда параметр ``state``, зашифрованный ключом
сервиса: ссылку выдаёт только бот и только админу, живёт она десять минут.

**Refresh-токен выдаётся не всегда.** Google отдаёт его только при первом согласии,
а на повторных возвращает лишь access-токен. Поэтому ``prompt=consent`` стоит
всегда: без него переподключение аккаунта тихо сохранило бы запись без
refresh-токена, и сервис умер бы через час — ровно тогда, когда истечёт access.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import secrets
import time

import sqlalchemy as sa
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from sqlalchemy.ext.asyncio import AsyncSession

from tutorsync.config import Settings, get_settings
from tutorsync.crypto import decrypt, encrypt, issued_at
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
#
# В state едет не только «эту ссылку выдали мы», но и code_verifier протокола
# PKCE. Причина техническая и неочевидная: google-auth-oauthlib генерирует
# верификатор внутри объекта Flow при построении ссылки, а колбэк прилетает
# отдельным HTTP-запросом, в котором Flow создаётся заново — и верификатор
# теряется. Google на обмене отвечает «Missing code verifier».
#
# Хранить его негде: сервер без сессий, а заводить таблицу ради строки, живущей
# десять минут, — лишняя сущность. Зато state Google возвращает дословно, и
# он подходит идеально: шифруем верификатор ключом сервиса и кладём туда.
#
# Отказаться от PKCE было бы проще (для web-клиента с секретом он необязателен),
# но он защищает ровно от того сценария, который здесь реален: перехвата кода
# авторизации из адресной строки или истории браузера.


def _pack_state(code_verifier: str) -> str:
    """Шифрует верификатор ключом сервиса.

    Fernet, а не подпись: значение внутри должно остаться нечитаемым. Он же
    несёт метку времени, по которой проверяется срок жизни ссылки, — отдельное
    поле с датой и отдельная подпись к нему были бы тем же самым, но руками.
    """
    return encrypt(code_verifier)


def verify_state(state: str, now: float | None = None) -> str:
    """Проверяет state и возвращает спрятанный в нём code_verifier.

    Бросает InvalidStateError, если state подделан, испорчен или просрочен.
    """
    try:
        verifier = decrypt(state)
    except Exception as exc:  # SecretUnreadableError и любой мусор в параметре
        raise InvalidStateError("ссылка повреждена или выдана не этим сервисом") from exc

    created = issued_at(state)
    if created is not None:
        if (now if now is not None else time.time()) - created > STATE_TTL_SEC:
            raise InvalidStateError("ссылка авторизации просрочена, запроси новую")
    return verifier


def make_code_verifier() -> str:
    """Случайная строка PKCE.

    Алфавит token_urlsafe (A–Z, a–z, 0–9, «-», «_») целиком входит в разрешённый
    RFC 7636, а длина попадает в требуемые 43–128 символов.
    """
    return secrets.token_urlsafe(64)


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


def build_auth_url() -> str:
    """Ссылка на экран согласия Google.

    Верификатор PKCE задаётся до построения ссылки, а не читается из Flow
    после неё: state передаётся параметром в тот же вызов, который верификатор
    и порождает, — взять его оттуда постфактум уже некуда.
    """
    settings = get_settings()
    flow = _flow(settings)
    flow.code_verifier = make_code_verifier()

    url, _ = flow.authorization_url(
        # offline — иначе refresh-токена не будет вовсе и сервис проживёт час.
        access_type="offline",
        # consent — иначе refresh-токен придёт только при самой первой
        # авторизации, а при переподключении молча не придёт.
        prompt="consent",
        include_granted_scopes="true",
        state=_pack_state(flow.code_verifier),
    )
    return str(url)


async def exchange_code(code: str, code_verifier: str) -> Credentials:
    """Меняет код авторизации на токены. Сетевой вызов — уводим из event loop."""
    settings = get_settings()
    flow = _flow(settings)
    # Тот же верификатор, что уехал в code_challenge при построении ссылки:
    # Google сверяет их между собой и без совпадения код не обменяет.
    flow.code_verifier = code_verifier

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
