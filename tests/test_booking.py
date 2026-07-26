"""Тесты записи урока.

Проверяется то, ради чего вообще существует единая точка входа: что урок,
буферы и защита от пересечений всегда появляются вместе, а не по отдельности.

Тесты идут на SQLite и потому проверяют именно предварительную проверку
занятости запросом. Констрейнт ``EXCLUDE`` в SQLite не существует — гонку
двух параллельных транзакций он ловит только в Postgres, и проверяется это
отдельно, на настоящей базе.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tutorsync.db.base import Base
from tutorsync.db.models import BusyInterval, Conflict, Lesson
from tutorsync.enums import IntervalRole, LessonSource
from tutorsync.services.booking import (
    SlotTakenError,
    create_lesson,
    overlapping_lesson_ids,
)

#: Понедельник, обычный рабочий день, вдали от переводов стрелок.
START = dt.datetime(2026, 9, 7, 10, 0, tzinfo=dt.UTC)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        yield db
    await engine.dispose()


@pytest.fixture
def settings(env):
    return env(
        GCAL_CALENDAR_PREPLY="preply@group.calendar.google.com",
        GCAL_CALENDAR_PRIVATE="private@group.calendar.google.com",
        GCAL_CALENDAR_BUFFERS="buffers@group.calendar.google.com",
        BUFFER_BEFORE_MIN="10",
        BUFFER_AFTER_MIN="10",
    )


async def _book(session, settings, *, start=START, duration=60, **kwargs) -> Lesson:
    return await create_lesson(
        session,
        settings,
        source=kwargs.pop("source", LessonSource.PRIVATE),
        start_utc=start,
        duration_min=duration,
        student_name=kwargs.pop("student_name", "Тест"),
        **kwargs,
    )


async def test_lesson_gets_lesson_interval_and_two_buffers(session, settings):
    lesson = await _book(session, settings)

    intervals = (await session.scalars(
        sa.select(BusyInterval).where(BusyInterval.lesson_id == lesson.id)
    )).all()
    by_role = {i.role: i for i in intervals}

    assert set(by_role) == {
        IntervalRole.LESSON,
        IntervalRole.BUFFER_BEFORE,
        IntervalRole.BUFFER_AFTER,
    }
    assert by_role[IntervalRole.LESSON].start_utc == START
    assert by_role[IntervalRole.LESSON].end_utc == START + dt.timedelta(minutes=60)


async def test_buffers_never_overlap_the_lesson(session, settings):
    lesson = await _book(session, settings)

    intervals = (await session.scalars(
        sa.select(BusyInterval).where(BusyInterval.lesson_id == lesson.id)
    )).all()
    by_role = {i.role: i for i in intervals}
    before, after = by_role[IntervalRole.BUFFER_BEFORE], by_role[IntervalRole.BUFFER_AFTER]

    # Буфер до заканчивается ровно в начале урока, буфер после начинается ровно
    # в его конце. Ни минуты пересечения — иначе урок сам себя объявил бы занятым.
    assert before.end_utc == lesson.start_utc
    assert before.start_utc == lesson.start_utc - dt.timedelta(minutes=10)
    assert after.start_utc == lesson.end_utc
    assert after.end_utc == lesson.end_utc + dt.timedelta(minutes=10)


async def test_zero_buffers_create_no_intervals(session, env):
    settings = env(
        GCAL_CALENDAR_PRIVATE="private@group.calendar.google.com",
        GCAL_CALENDAR_BUFFERS="buffers@group.calendar.google.com",
        BUFFER_BEFORE_MIN="0",
        BUFFER_AFTER_MIN="0",
    )

    lesson = await _book(session, settings)

    roles = (await session.scalars(
        sa.select(BusyInterval.role).where(BusyInterval.lesson_id == lesson.id)
    )).all()
    # Нулевой буфер — это «буферов нет», а не событие нулевой длины.
    assert list(roles) == [IntervalRole.LESSON]


async def test_overlapping_booking_rejected(session, settings):
    first = await _book(session, settings)

    with pytest.raises(SlotTakenError) as exc:
        await _book(session, settings, start=START + dt.timedelta(minutes=30))

    assert exc.value.lesson_ids == [first.id]


async def test_touching_lessons_allowed(session, settings):
    """Урок, начинающийся ровно в момент окончания предыдущего, не пересекается.

    Границы полуоткрытые. Если бы совпадение точек считалось пересечением,
    расписание встык стало бы невозможным.
    """
    first = await _book(session, settings)

    second = await _book(session, settings, start=first.end_utc)

    assert second.id != first.id
    assert not second.conflict_accepted


async def test_buffers_do_not_block_neighbouring_lessons(session, settings):
    """Пересечение буферов друг с другом — норма, а не конфликт.

    Буфер после первого урока накрывает время, куда попадает буфер до второго.
    Проверка занятости смотрит только интервалы самого урока, поэтому вторая
    бронь проходит.
    """
    first = await _book(session, settings)

    second = await _book(session, settings, start=first.end_utc)

    overlapping_buffers = (await session.scalars(
        sa.select(sa.func.count()).select_from(BusyInterval).where(
            BusyInterval.role != IntervalRole.LESSON,
            BusyInterval.start_utc < second.start_utc,
            BusyInterval.end_utc > first.end_utc,
        )
    )).all()
    assert overlapping_buffers  # буферы действительно накладываются
    assert second.id != first.id  # и это не помешало записи


async def test_preply_lesson_accepted_over_existing_booking(session, settings):
    """Оплаченный урок с Preply принимается поверх брони, но с пометкой.

    Отказать ему нельзя — деньги уже взяты, и урок состоится независимо от
    того, что думает наша база. Отменять что-либо автоматически тоже нельзя,
    поэтому создаётся запись о конфликте для решения вручную.
    """
    private = await _book(session, settings)

    preply = await _book(
        session,
        settings,
        start=START + dt.timedelta(minutes=15),
        source=LessonSource.PREPLY,
        external_id="preply-1",
        accept_conflict=True,
    )

    assert preply.conflict_accepted
    conflicts = (await session.scalars(sa.select(Conflict))).all()
    assert len(conflicts) == 1
    assert {conflicts[0].lesson_a_id, conflicts[0].lesson_b_id} == {private.id, preply.id}


async def test_cancelled_lesson_frees_the_slot(session, settings):
    """Снятый интервал перестаёт занимать время."""
    first = await _book(session, settings)
    await session.execute(
        sa.update(BusyInterval)
        .where(BusyInterval.lesson_id == first.id)
        .values(active=False)
    )
    await session.flush()

    assert await overlapping_lesson_ids(session, START, START + dt.timedelta(minutes=60)) == []


async def test_naive_datetime_rejected(session, settings):
    with pytest.raises(ValueError, match="aware"):
        await create_lesson(
            session,
            settings,
            source=LessonSource.PRIVATE,
            start_utc=dt.datetime(2026, 9, 7, 10, 0),
            duration_min=60,
        )
