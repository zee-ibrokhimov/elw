"""Раскладка урока по событиям календаря.

Тесты идут против чистой функции, без базы и без Google: правила «сколько
событий и в какие календари» ошибиться проще всего именно на границах —
выключенные буферы, отменённый урок, урок с Preply против частного.
"""

from __future__ import annotations

import datetime as dt

import pytest

from tutorsync.db.models import Lesson
from tutorsync.enums import CalendarKey, EventRole, LessonSource, LessonStatus
from tutorsync.gcal.events import PROP_LESSON_ID, PROP_ROLE, PROP_VERSION, plan_events

CALENDARS = {
    "GCAL_CALENDAR_PREPLY": "preply@group.calendar.google.com",
    "GCAL_CALENDAR_PRIVATE": "private@group.calendar.google.com",
    "GCAL_CALENDAR_BUFFERS": "buffers@group.calendar.google.com",
}
START = dt.datetime(2026, 8, 7, 15, 0, tzinfo=dt.UTC)


def make_lesson(source: LessonSource = LessonSource.PRIVATE, **kwargs) -> Lesson:
    lesson = Lesson(
        id=kwargs.pop("id", 1),
        source=source,
        start_utc=START,
        end_utc=START + dt.timedelta(minutes=50),
        duration_min=50,
        status=LessonStatus.SCHEDULED,
        sync_version=kwargs.pop("sync_version", 1),
        student_name_raw=kwargs.pop("student_name_raw", "Sergey"),
    )
    for key, value in kwargs.items():
        setattr(lesson, key, value)
    return lesson


def roles(planned) -> set[EventRole]:
    return {item.role for item in planned}


def test_private_lesson_goes_to_private_calendar(env):
    settings = env(**CALENDARS, BUFFER_BEFORE_MIN="0", BUFFER_AFTER_MIN="0")
    planned = plan_events(make_lesson(), settings)

    assert len(planned) == 1
    assert planned[0].calendar_key is CalendarKey.PRIVATE
    assert planned[0].calendar_id == CALENDARS["GCAL_CALENDAR_PRIVATE"]


def test_preply_lesson_goes_to_preply_calendar(env):
    settings = env(**CALENDARS, BUFFER_BEFORE_MIN="0", BUFFER_AFTER_MIN="0")
    planned = plan_events(make_lesson(LessonSource.PREPLY), settings)

    assert planned[0].calendar_key is CalendarKey.PREPLY
    assert planned[0].body["summary"].startswith("Preply:")


def test_zero_buffers_produce_no_buffer_events(env):
    """Нулевой буфер — это отсутствие события, а не событие нулевой длины."""
    settings = env(**CALENDARS, BUFFER_BEFORE_MIN="0", BUFFER_AFTER_MIN="0")
    assert roles(plan_events(make_lesson(), settings)) == {EventRole.LESSON}


def test_buffers_surround_the_lesson(env):
    settings = env(**CALENDARS, BUFFER_BEFORE_MIN="10", BUFFER_AFTER_MIN="15")
    planned = {item.role: item for item in plan_events(make_lesson(), settings)}

    assert set(planned) == {EventRole.LESSON, EventRole.BUFFER_BEFORE, EventRole.BUFFER_AFTER}
    before, after = planned[EventRole.BUFFER_BEFORE], planned[EventRole.BUFFER_AFTER]
    # Буферы примыкают к уроку и не накрывают его самого.
    assert before.end_utc == START
    assert before.start_utc == START - dt.timedelta(minutes=10)
    assert after.start_utc == START + dt.timedelta(minutes=50)
    assert after.end_utc == START + dt.timedelta(minutes=65)
    assert before.calendar_key is CalendarKey.BUFFERS


def test_buffers_skipped_without_buffer_calendar(env):
    """Буферы включены, но календаря для них нет — событий быть не должно."""
    settings = env(
        GCAL_CALENDAR_PREPLY=CALENDARS["GCAL_CALENDAR_PREPLY"],
        GCAL_CALENDAR_PRIVATE=CALENDARS["GCAL_CALENDAR_PRIVATE"],
        BUFFER_BEFORE_MIN="10",
        BUFFER_AFTER_MIN="10",
    )
    assert roles(plan_events(make_lesson(), settings)) == {EventRole.LESSON}


def test_busy_mirror_only_for_non_preply(env):
    settings = env(
        **CALENDARS, BUFFER_BEFORE_MIN="0", BUFFER_AFTER_MIN="0", PREPLY_BUSY_MIRROR="true"
    )
    assert EventRole.BUSY_MIRROR in roles(plan_events(make_lesson(), settings))
    # Дубль урока с Preply в календаре занятости заставил бы площадку считать
    # своё же время занятым дважды.
    assert EventRole.BUSY_MIRROR not in roles(
        plan_events(make_lesson(LessonSource.PREPLY), settings)
    )


def test_event_carries_identity_for_reconcile(env):
    settings = env(**CALENDARS, BUFFER_BEFORE_MIN="0", BUFFER_AFTER_MIN="0")
    props = plan_events(make_lesson(id=42, sync_version=7), settings)[0].body[
        "extendedProperties"
    ]["private"]

    assert props[PROP_LESSON_ID] == "42"
    assert props[PROP_ROLE] == EventRole.LESSON.value
    # Версия нужна сверке, чтобы отличить отставшее событие от изменённого руками.
    assert props[PROP_VERSION] == "7"


def test_times_are_utc_with_explicit_zone(env):
    """Без явного пояса Google истолкует время в поясе календаря."""
    settings = env(**CALENDARS, BUFFER_BEFORE_MIN="0", BUFFER_AFTER_MIN="0")
    body = plan_events(make_lesson(), settings)[0].body

    assert body["start"]["timeZone"] == "UTC"
    assert body["start"]["dateTime"] == "2026-08-07T15:00:00+00:00"
    assert body["end"]["dateTime"] == "2026-08-07T15:50:00+00:00"


def test_lesson_is_opaque_and_silent(env):
    settings = env(**CALENDARS, BUFFER_BEFORE_MIN="0", BUFFER_AFTER_MIN="0")
    body = plan_events(make_lesson(), settings)[0].body

    # transparent означало бы «свободен», и Preply поставил бы урок поверх.
    assert body["transparency"] == "opaque"
    # Напоминания шлёт бот; всплывашки Google дублировали бы их.
    assert body["reminders"] == {"useDefault": False, "overrides": []}


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (LessonSource.PREPLY, "Preply: Sergey"),
        (LessonSource.PRIVATE, "Урок: Sergey"),
    ],
)
def test_title_says_where_the_lesson_came_from(env, source, expected):
    settings = env(**CALENDARS, BUFFER_BEFORE_MIN="0", BUFFER_AFTER_MIN="0")
    assert plan_events(make_lesson(source), settings)[0].body["summary"] == expected


def test_block_uses_its_note_as_title(env):
    settings = env(**CALENDARS, BUFFER_BEFORE_MIN="0", BUFFER_AFTER_MIN="0")
    lesson = make_lesson(LessonSource.BLOCK, student_name_raw=None, note="врач")
    assert plan_events(lesson, settings)[0].body["summary"] == "врач"
