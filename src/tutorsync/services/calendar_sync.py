"""Приведение Google Calendar в соответствие с базой.

База — источник истины, календарь — её проекция. Поэтому здесь нет операций
«создать событие» и «удалить событие» как отдельных сценариев: есть одна
функция, которая смотрит, что должно быть, сравнивает с тем, что уже заведено,
и ставит в outbox разницу.

Такая форма выбрана не из любви к декларативности. Сценарный код («забронировали →
создать», «отменили → удалить», «перенесли → подвинуть») расходится с реальностью
на первом же частичном сбое: событие создалось, а запись о нём — нет, и дальше
каждый сценарий должен помнить про этот случай. Сравнение желаемого с текущим
одинаково отрабатывает и первый запуск, и починку после сбоя.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from tutorsync.config import Settings, get_settings
from tutorsync.db.models import CalendarEvent, Lesson
from tutorsync.enums import LessonStatus, OutboxKind
from tutorsync.gcal.events import PlannedEvent, plan_events
from tutorsync.logging import get_logger
from tutorsync.services.outbox import enqueue

log = get_logger(__name__)


def _planned_for(lesson: Lesson, settings: Settings) -> list[PlannedEvent]:
    """Что должно быть в календаре для этого урока.

    У отменённого и завершённого урока — ничего: событий быть не должно, и
    существующие подлежат удалению. Отдельной ветки «отмена» из-за этого не нужно.
    """
    if lesson.status is not LessonStatus.SCHEDULED:
        return []
    return plan_events(lesson, settings)


async def sync_lesson(session: AsyncSession, lesson: Lesson) -> int:
    """Ставит в outbox всё, что нужно сделать в календаре для этого урока.

    Возвращает число поставленных заданий. Ничего не отправляет наружу — вызов
    безопасен внутри транзакции, в этом и смысл.
    """
    settings = get_settings()
    planned = _planned_for(lesson, settings)

    existing = list(
        (
            await session.execute(
                sa.select(CalendarEvent).where(CalendarEvent.lesson_id == lesson.id)
            )
        )
        .scalars()
        .all()
    )
    by_slot = {(row.calendar_key, row.role): row for row in existing}
    queued = 0

    for item in planned:
        row = by_slot.pop((item.calendar_key, item.role), None)

        if row is None:
            await enqueue(
                session,
                OutboxKind.GCAL_CREATE_EVENT,
                {
                    "lesson_id": lesson.id,
                    "calendar_key": item.calendar_key.value,
                    "calendar_id": item.calendar_id,
                    "role": item.role.value,
                    "sync_version": lesson.sync_version,
                    "body": item.body,
                },
                # Ключ без версии: пока задание на создание ждёт, вторая правка
                # урока не должна порождать второе событие — она догонит его
                # заданием на правку после того, как событие появится.
                dedup_key=f"gcal:create:{lesson.id}:{item.calendar_key.value}:{item.role.value}",
            )
            queued += 1
            continue

        if row.sync_version >= lesson.sync_version:
            continue

        await enqueue(
            session,
            OutboxKind.GCAL_PATCH_EVENT,
            {
                "calendar_event_id": row.id,
                "calendar_id": row.gcal_calendar_id,
                "event_id": row.gcal_event_id,
                "sync_version": lesson.sync_version,
                "body": item.body,
            },
            # Версия в ключе: две последовательные правки — два разных задания,
            # иначе вторая склеилась бы с первой и потерялась.
            dedup_key=f"gcal:patch:{row.id}:{lesson.sync_version}",
        )
        queued += 1

    # Всё, что осталось в by_slot, календарю больше не нужно: урок отменён,
    # буферы выключены настройкой или изменилась раскладка по календарям.
    for row in by_slot.values():
        await enqueue(
            session,
            OutboxKind.GCAL_DELETE_EVENT,
            {
                "calendar_event_id": row.id,
                "calendar_id": row.gcal_calendar_id,
                "event_id": row.gcal_event_id,
            },
            dedup_key=f"gcal:delete:{row.id}",
        )
        queued += 1

    log.debug(
        "gcal.sync_lesson.planned",
        lesson_id=lesson.id,
        status=lesson.status.value,
        events=len(planned),
        queued=queued,
    )
    return queued


async def bump_and_sync(session: AsyncSession, lesson: Lesson) -> int:
    """Отмечает урок изменённым и ставит синхронизацию.

    sync_version инкрементируется здесь, а не в вызывающем коде, чтобы нельзя
    было изменить урок и забыть поднять версию: событие в календаре тогда
    осталось бы старым, и сверка сочла бы это нормой.
    """
    lesson.sync_version += 1
    await session.flush()
    return await sync_lesson(session, lesson)
