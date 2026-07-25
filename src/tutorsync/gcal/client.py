"""Асинхронная обёртка над Google Calendar API.

google-api-python-client синхронный и блокирующий. Внутри процесса, где живут
и веб, и планировщик, любой его вызов напрямую из корутины останавливает весь
event loop на время сетевого запроса — поэтому каждый уход в Google обёрнут
в ``asyncio.to_thread``.

Второе, что здесь важно, — деление ошибок на временные и постоянные. Для outbox
это не стилистика, а поведение: временную ошибку надо повторить (Google лежит,
кончилась квота), постоянную повторять бессмысленно и вредно — сто ретраев
события с битым телом просто сожгут квоту и зальют лог.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from tutorsync.logging import get_logger

log = get_logger(__name__)

#: Коды, при которых повтор имеет смысл: перегрузка, квота, внутренние сбои Google.
_RETRYABLE_STATUS = {403, 429, 500, 502, 503, 504}
#: Причины в теле 403: превышение квоты — временно, отсутствие прав — навсегда.
_RETRYABLE_403_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "backendError"}


class GCalError(RuntimeError):
    """Базовая ошибка обращения к календарю."""


class TransientGCalError(GCalError):
    """Повторить позже: Google недоступен или срезал по квоте."""


class PermanentGCalError(GCalError):
    """Повторять бесполезно: битый запрос, нет прав, календарь не существует."""


class EventGoneError(PermanentGCalError):
    """События уже нет. Для удаления это успех, а не ошибка."""


def _classify(exc: HttpError) -> GCalError:
    status = getattr(exc.resp, "status", None)
    reason = ""
    try:
        details = exc.error_details
        if isinstance(details, list) and details:
            reason = str(details[0].get("reason", ""))
    # Разбор диагностики не должен ронять обработку: если Google поменяет
    # формат error_details, важна сама ошибка, а не её причина.
    except Exception:
        reason = ""

    if status in (404, 410):
        return EventGoneError(f"{status}: {exc}")
    if status == 403 and reason not in _RETRYABLE_403_REASONS:
        return PermanentGCalError(f"403 {reason or 'forbidden'}: {exc}")
    if status in _RETRYABLE_STATUS:
        return TransientGCalError(f"{status} {reason}: {exc}")
    return PermanentGCalError(f"{status}: {exc}")


async def _call(request: Any) -> Any:
    """Выполняет подготовленный запрос googleapiclient вне event loop."""
    try:
        return await asyncio.to_thread(request.execute)
    except HttpError as exc:
        raise _classify(exc) from exc
    except TimeoutError as exc:
        raise TransientGCalError(f"таймаут: {exc}") from exc
    except OSError as exc:
        # Сеть отвалилась — это ровно тот случай, ради которого нужен ретрай.
        raise TransientGCalError(f"сетевая ошибка: {exc}") from exc


class CalendarClient:
    """Тонкий фасад: ровно те вызовы, которые нужны сервису, и ни одного лишнего."""

    def __init__(self, credentials: Credentials) -> None:
        # cache_discovery=False — иначе клиент пытается писать кэш discovery
        # на диск, а контейнер запущен не от root и файловая система только
        # для чтения по замыслу образа.
        self._service = build(
            "calendar", "v3", credentials=credentials, cache_discovery=False
        )

    # --- аккаунт ------------------------------------------------------------

    async def account_email(self) -> str:
        """Адрес авторизованного аккаунта.

        Берётся из id основного календаря, а не из id_token: так не приходится
        просить у пользователя скоупы openid/email ради одной строки на экране
        согласия — чем меньше запрошено, тем меньше поводов отказать.
        """
        data = await _call(self._service.calendars().get(calendarId="primary"))
        return str(data["id"])

    async def calendar_exists(self, calendar_id: str) -> bool:
        try:
            await _call(self._service.calendars().get(calendarId=calendar_id))
        except (EventGoneError, PermanentGCalError):
            return False
        return True

    # --- события ------------------------------------------------------------

    async def insert_event(self, calendar_id: str, body: dict[str, Any]) -> dict[str, Any]:
        data = await _call(
            self._service.events().insert(calendarId=calendar_id, body=body, sendUpdates="none")
        )
        return dict(data)

    async def patch_event(
        self, calendar_id: str, event_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Обновляет событие.

        Оптимистичной блокировки по If-Match здесь нет сознательно:
        googleapiclient не даёт задать заголовок отдельного запроса, не залезая
        во внутренности транспорта. Расхождение с ручной правкой ловится не тут,
        а сверкой — она сравнивает sync_version в extendedProperties события
        с версией урока в базе, и это работает независимо от того, кто и когда
        событие менял.
        """
        data = await _call(
            self._service.events().patch(
                calendarId=calendar_id,
                eventId=event_id,
                body=body,
                sendUpdates="none",
            )
        )
        return dict(data)

    async def delete_event(self, calendar_id: str, event_id: str) -> None:
        """Удаляет событие. Отсутствие события считается успехом.

        Иначе повторная доставка задания на удаление (а outbox по устройству
        может доставить дважды) навсегда застревала бы в ошибке.
        """
        try:
            await _call(
                self._service.events().delete(
                    calendarId=calendar_id, eventId=event_id, sendUpdates="none"
                )
            )
        except EventGoneError:
            log.debug("gcal.delete.already_gone", calendar_id=calendar_id, event_id=event_id)

    async def get_event(self, calendar_id: str, event_id: str) -> dict[str, Any] | None:
        try:
            return dict(await _call(
                self._service.events().get(calendarId=calendar_id, eventId=event_id)
            ))
        except EventGoneError:
            return None

    async def list_events(
        self,
        calendar_id: str,
        time_min: dt.datetime,
        time_max: dt.datetime,
        *,
        single_events: bool = True,
    ) -> list[dict[str, Any]]:
        """Все события календаря в окне, с раскрытыми повторами.

        singleEvents=True разворачивает серии в отдельные вхождения — иначе
        еженедельный урок пришёл бы одной записью с правилом повтора, и считать
        занятость пришлось бы самим, повторяя логику RRULE со всеми исключениями.
        """
        params: dict[str, Any] = {
            "calendarId": calendar_id,
            "timeMin": time_min.astimezone(dt.UTC).isoformat(),
            "timeMax": time_max.astimezone(dt.UTC).isoformat(),
            "singleEvents": single_events,
            "maxResults": 2500,
        }
        # orderBy=startTime разрешён только вместе с singleEvents — с сериями
        # Google отвечает 400, поэтому параметр добавляется, а не обнуляется.
        if single_events:
            params["orderBy"] = "startTime"

        items: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            data = await _call(self._service.events().list(**params, pageToken=page_token))
            items.extend(data.get("items", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                return items
