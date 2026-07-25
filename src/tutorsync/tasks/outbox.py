"""Исполнитель очереди внешних эффектов.

Забирает готовые задания и выполняет их: создаёт, правит и удаляет события
в Google Calendar, шлёт сообщения в Telegram. Каждое задание обрабатывается
в собственной транзакции — падение одного не должно откатывать соседние.

Порядок внутри задания важен и обратен интуиции: сначала вызов наружу, потом
запись результата в базу. Обратный порядок (записали, потом позвонили) оставлял
бы после сбоя запись о событии, которого в календаре нет, — а это хуже, чем
повторный вызов: повтор Google переживёт, а выдуманный event_id будет ломать
каждую последующую правку.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from tutorsync.config import get_settings
from tutorsync.db.models import CalendarEvent, ChannelHealth, OutboxJob
from tutorsync.db.session import session_scope
from tutorsync.enums import CalendarKey, EventRole, OutboxKind, SyncChannel
from tutorsync.gcal.client import CalendarClient, PermanentGCalError, TransientGCalError
from tutorsync.gcal.oauth import NoCredentialsError, load_credentials
from tutorsync.logging import get_logger, trace
from tutorsync.services import notify
from tutorsync.services.outbox import (
    MAX_ATTEMPTS,
    claim_jobs,
    mark_done,
    mark_failed,
    mark_retry,
)

log = get_logger(__name__)

#: Сколько заданий берём за один проход. Ограничение осмысленное: при завале
#: очереди воркер не должен на полчаса переставать делать всё остальное.
BATCH_SIZE = 20


async def _client() -> CalendarClient:
    async with session_scope() as session:
        creds = await load_credentials(session)
    return CalendarClient(creds)


async def _handle_gcal_create(
    client: CalendarClient, session: Any, payload: dict[str, Any]
) -> None:
    created = await client.insert_event(payload["calendar_id"], payload["body"])
    session.add(
        CalendarEvent(
            lesson_id=payload["lesson_id"],
            calendar_key=CalendarKey(payload["calendar_key"]),
            gcal_calendar_id=payload["calendar_id"],
            gcal_event_id=created["id"],
            role=EventRole(payload["role"]),
            sync_version=payload["sync_version"],
            etag=created.get("etag"),
            last_synced_at=dt.datetime.now(dt.UTC),
        )
    )


async def _handle_gcal_patch(client: CalendarClient, session: Any, payload: dict[str, Any]) -> None:
    updated = await client.patch_event(
        payload["calendar_id"], payload["event_id"], payload["body"]
    )
    row = await session.get(CalendarEvent, payload["calendar_event_id"])
    if row is None:
        # Событие в календаре обновлено, а связи в базе уже нет: урок успели
        # удалить. Осиротевшее событие подберёт сверка — здесь ошибки нет.
        log.warning("outbox.patch.row_missing", event_id=payload["event_id"])
        return
    row.sync_version = payload["sync_version"]
    row.etag = updated.get("etag")
    row.last_synced_at = dt.datetime.now(dt.UTC)


async def _handle_gcal_delete(
    client: CalendarClient, session: Any, payload: dict[str, Any]
) -> None:
    await client.delete_event(payload["calendar_id"], payload["event_id"])
    row = await session.get(CalendarEvent, payload["calendar_event_id"])
    if row is not None:
        await session.delete(row)


async def _handle_tg_admin(payload: dict[str, Any]) -> None:
    await notify.send_admin(payload["text"])


async def _execute(job: OutboxJob, session: Any, client_holder: dict[str, CalendarClient]) -> None:
    """Выполняет одно задание. Исключение означает неуспех и разбирается снаружи."""
    if job.kind is OutboxKind.TG_NOTIFY_ADMIN:
        await _handle_tg_admin(job.payload)
        return
    if job.kind is OutboxKind.TG_NOTIFY_STUDENT:
        # Появится вместе с ботом для учеников (этап 4). До тех пор задание
        # такого рода поставить неоткуда, и молча «выполнять» его нельзя.
        raise PermanentGCalError("TG_NOTIFY_STUDENT ещё не реализован")

    # Клиент строится лениво и один раз на проход: каждый CalendarClient — это
    # загрузка discovery-документа, делать её на каждое задание расточительно.
    if "client" not in client_holder:
        client_holder["client"] = await _client()
    client = client_holder["client"]

    match job.kind:
        case OutboxKind.GCAL_CREATE_EVENT:
            await _handle_gcal_create(client, session, job.payload)
        case OutboxKind.GCAL_PATCH_EVENT:
            await _handle_gcal_patch(client, session, job.payload)
        case OutboxKind.GCAL_DELETE_EVENT:
            await _handle_gcal_delete(client, session, job.payload)
        case _:
            raise PermanentGCalError(f"неизвестный вид задания {job.kind!r}")


async def process_once() -> int:
    """Один проход по очереди. Возвращает число обработанных заданий."""
    settings = get_settings()
    client_holder: dict[str, CalendarClient] = {}

    async with session_scope() as session:
        jobs = await claim_jobs(session, BATCH_SIZE, is_postgres=settings.is_postgres)
        job_ids = [job.id for job in jobs]

    if not job_ids:
        return 0

    handled = 0
    for job_id in job_ids:
        async with session_scope() as session:
            job = await session.get(OutboxJob, job_id)
            if job is None:
                continue
            with trace(channel="outbox", job_id=job.id, kind=job.kind.value):
                try:
                    await _execute(job, session, client_holder)
                except NoCredentialsError as exc:
                    # Не ошибка задания, а незавершённая настройка. Ретраим —
                    # после авторизации очередь разберётся сама, без ручного
                    # перезапуска и без потери накопленных заданий.
                    await mark_retry(session, job, str(exc))
                    log.warning("outbox.no_credentials", job_id=job.id)
                except TransientGCalError as exc:
                    if job.attempts + 1 >= MAX_ATTEMPTS:
                        await mark_failed(session, job, str(exc))
                        await _alert_failed(job, str(exc))
                    else:
                        await mark_retry(session, job, str(exc))
                        log.warning("outbox.retry", job_id=job.id, attempts=job.attempts)
                # Падать воркеру нельзя: одно битое задание не должно
                # останавливать разбор очереди.
                except Exception as exc:
                    # Постоянные ошибки не ретраим: повторять запрос, который
                    # Google уже отверг по существу, — только жечь квоту.
                    await mark_failed(session, job, f"{type(exc).__name__}: {exc}")
                    await _alert_failed(job, f"{type(exc).__name__}: {exc}")
                    log.error("outbox.failed", job_id=job.id, error=str(exc))
                else:
                    await mark_done(session, job)
                    handled += 1

    async with session_scope() as session:
        await _mark_channel(session, SyncChannel.OUTBOX)

    return handled


async def _alert_failed(job: OutboxJob, error: str) -> None:
    await notify.send_admin(
        "⚠️ Задание синхронизации не выполнено и снято с очереди.\n"
        f"Тип: <code>{job.kind.value}</code>\n"
        f"Попыток: {job.attempts}\n"
        f"Ошибка: <code>{error[:400]}</code>"
    )


async def _mark_channel(session: Any, channel: SyncChannel) -> None:
    now = dt.datetime.now(dt.UTC)
    row = await session.get(ChannelHealth, channel)
    if row is None:
        session.add(ChannelHealth(channel=channel, last_success_at=now, consecutive_errors=0))
        return
    row.last_success_at = now
    row.consecutive_errors = 0
    row.alerted_at = None
