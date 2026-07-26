"""Единственная точка, через которую расписание меняется.

Через неё проходит всё: бронь из бота, урок из письма Preply, блок от админа.
Смысл в том, чтобы проверка занятости, запись интервалов и постановка задач
на календарь происходили в одной транзакции. Разложи это по вызывающим местам —
и рано или поздно появится урок, записанный без буферов, или бронь, прошедшая
проверку, которая устарела за миллисекунду до вставки.

Занятость проверяется дважды и это не дублирование:

* запросом перед вставкой — чтобы ученик получил понятное «слот занят»,
  а не текст про нарушение констрейнта;
* констрейнтом базы при вставке — потому что между запросом и вставкой
  успевает вклиниться параллельная транзакция, и только база решает этот спор.

Оплаченный урок с Preply — отдельный случай. Отказать ему нельзя: деньги уже
взяты, и урок состоится независимо от того, что думает наша база. Поэтому он
принимается поверх существующей брони с пометкой ``conflict_accepted``, а рядом
заводится запись в ``conflicts``, по которой админ получает обе брони и решает
сам. Автоматически не отменяется ничего.
"""

from __future__ import annotations

import datetime as dt

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tutorsync.config import Settings
from tutorsync.db.models import BusyInterval, Conflict, Lesson
from tutorsync.enums import IntervalRole, LessonSource, LessonStatus
from tutorsync.logging import get_logger
from tutorsync.services.calendar_sync import sync_lesson

log = get_logger(__name__)


class SlotTakenError(RuntimeError):
    """Время занято. Несёт идентификаторы мешающих уроков — их показывают админу."""

    def __init__(self, lesson_ids: list[int]) -> None:
        self.lesson_ids = lesson_ids
        super().__init__(f"Время пересекается с уроками: {lesson_ids or 'неизвестно'}")


async def overlapping_lesson_ids(
    session: AsyncSession,
    start_utc: dt.datetime,
    end_utc: dt.datetime,
    *,
    exclude_lesson_id: int | None = None,
) -> list[int]:
    """Уроки, занимающие это время.

    Сравниваются только интервалы роли LESSON: буферы соседних уроков при
    плотной сетке законно накладываются друг на друга, и считать их занятостью
    для этой проверки означало бы запретить нормальное расписание.

    Границы полуоткрыты: урок, начинающийся ровно в момент окончания
    предыдущего, не пересекается с ним.
    """
    stmt = sa.select(BusyInterval.lesson_id).where(
        BusyInterval.role == IntervalRole.LESSON,
        BusyInterval.active.is_(True),
        BusyInterval.start_utc < end_utc,
        BusyInterval.end_utc > start_utc,
    )
    if exclude_lesson_id is not None:
        stmt = stmt.where(BusyInterval.lesson_id != exclude_lesson_id)
    rows = await session.scalars(stmt)
    return sorted(set(rows.all()))


def buffer_bounds(
    lesson: Lesson, settings: Settings
) -> list[tuple[IntervalRole, dt.datetime, dt.datetime]]:
    """Границы буферов — строго до и после урока, сам урок не перекрывается."""
    bounds: list[tuple[IntervalRole, dt.datetime, dt.datetime]] = []
    if settings.buffer_before_min > 0:
        bounds.append((
            IntervalRole.BUFFER_BEFORE,
            lesson.start_utc - dt.timedelta(minutes=settings.buffer_before_min),
            lesson.start_utc,
        ))
    if settings.buffer_after_min > 0:
        bounds.append((
            IntervalRole.BUFFER_AFTER,
            lesson.end_utc,
            lesson.end_utc + dt.timedelta(minutes=settings.buffer_after_min),
        ))
    return bounds


async def create_lesson(
    session: AsyncSession,
    settings: Settings,
    *,
    source: LessonSource,
    start_utc: dt.datetime,
    duration_min: int,
    student_id: int | None = None,
    student_name: str | None = None,
    external_id: str | None = None,
    booked_tz: str | None = None,
    note: str | None = None,
    accept_conflict: bool = False,
) -> Lesson:
    """Записывает урок вместе с буферами и ставит синхронизацию календаря.

    ``accept_conflict`` включается только для источников, которым нельзя
    отказать — то есть для Preply. Для брони из бота он всегда False: там
    отказ безобиден, ученик просто выберет другое время.

    Внешние вызовы отсюда не делаются: в календарь и в Telegram уходят задания
    через outbox, которые исполняются уже после коммита.
    """
    if start_utc.tzinfo is None:
        raise ValueError("start_utc должен быть aware datetime")
    if duration_min <= 0:
        raise ValueError("Длительность урока должна быть положительной")

    start_utc = start_utc.astimezone(dt.UTC)
    end_utc = start_utc + dt.timedelta(minutes=duration_min)

    conflicts_with = await overlapping_lesson_ids(session, start_utc, end_utc)
    if conflicts_with and not accept_conflict:
        raise SlotTakenError(conflicts_with)

    lesson = Lesson(
        source=source,
        external_id=external_id,
        student_id=student_id,
        student_name_raw=student_name,
        start_utc=start_utc,
        end_utc=end_utc,
        duration_min=duration_min,
        status=LessonStatus.SCHEDULED,
        booked_tz=booked_tz or settings.owner_tz,
        note=note,
        conflict_accepted=bool(conflicts_with),
        sync_version=1,
    )
    session.add(lesson)
    await session.flush()

    session.add(BusyInterval(
        lesson_id=lesson.id,
        role=IntervalRole.LESSON,
        start_utc=start_utc,
        end_utc=end_utc,
        active=True,
        conflict_accepted=lesson.conflict_accepted,
    ))
    for role, buf_start, buf_end in buffer_bounds(lesson, settings):
        session.add(BusyInterval(
            lesson_id=lesson.id,
            role=role,
            start_utc=buf_start,
            end_utc=buf_end,
            active=True,
            conflict_accepted=lesson.conflict_accepted,
        ))

    try:
        await session.flush()
    except IntegrityError as exc:
        # Сюда попадает то, что не увидела проверка выше: параллельная
        # транзакция заняла то же время между запросом и вставкой. Спор решает
        # констрейнт базы, и его решение окончательно.
        raise SlotTakenError(conflicts_with) from exc

    for other_id in conflicts_with:
        session.add(Conflict(lesson_a_id=other_id, lesson_b_id=lesson.id))
    if conflicts_with:
        log.warning(
            "booking.conflict_accepted",
            lesson_id=lesson.id,
            source=source.value,
            conflicts_with=conflicts_with,
        )

    await sync_lesson(session, lesson)
    log.info(
        "booking.created",
        lesson_id=lesson.id,
        source=source.value,
        start_utc=start_utc.isoformat(),
        duration_min=duration_min,
    )
    return lesson
