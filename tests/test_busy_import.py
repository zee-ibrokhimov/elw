"""Отбор занятости из чужих календарей.

Это единственное место, где ошибка не заметна вообще: лишний отброшенный
интервал превращается в слот, который бот продаст поверх настоящего урока,
и узнают об этом двое учеников одновременно. Поэтому правила проверяются
на образцах ответов Google, а не на живом календаре.
"""

from __future__ import annotations

import datetime as dt

import pytest

from tutorsync.services.busy_import import busy_windows, import_window

CAL = "preply@group.calendar.google.com"


def event(**kwargs) -> dict:
    """Событие в том виде, в каком его отдаёт Google Calendar API."""
    base = {
        "id": kwargs.pop("id", "evt1"),
        "summary": kwargs.pop("summary", "Sergey - Preply lesson"),
        "start": {"dateTime": kwargs.pop("start", "2026-08-07T17:00:00+02:00")},
        "end": {"dateTime": kwargs.pop("end", "2026-08-07T17:50:00+02:00")},
    }
    base.update(kwargs)
    return base


def test_timed_event_becomes_busy(env):
    settings = env()
    (window,) = busy_windows([event()], CAL, settings)

    assert window.title == "Sergey - Preply lesson"
    # 17:00 в Риме летом — это 15:00 UTC; хранится всё в UTC.
    assert window.start_utc == dt.datetime(2026, 8, 7, 15, 0, tzinfo=dt.UTC)
    assert window.end_utc == dt.datetime(2026, 8, 7, 15, 50, tzinfo=dt.UTC)


def test_transparent_event_is_free(env):
    """Владелец календаря пометил себя свободным — уважаем пометку."""
    settings = env()
    assert busy_windows([event(transparency="transparent")], CAL, settings) == []


def test_cancelled_event_ignored(env):
    """Отменённые вхождения серий Google продолжает отдавать."""
    settings = env()
    assert busy_windows([event(status="cancelled")], CAL, settings) == []


def test_own_events_ignored(env):
    """Управляемый календарь в списке читаемых удвоил бы занятость."""
    settings = env()
    ours = event(extendedProperties={"private": {"tutorsync_lesson_id": "7"}})
    assert busy_windows([ours], CAL, settings) == []


def test_all_day_event_ignored_by_default(env):
    """День рождения не должен закрывать сутки целиком."""
    settings = env()
    birthday = {
        "id": "bd",
        "summary": "День рождения",
        "start": {"date": "2026-08-10"},
        "end": {"date": "2026-08-11"},
    }
    assert busy_windows([birthday], CAL, settings) == []


def test_all_day_event_can_be_enabled(env):
    settings = env(BUSY_INCLUDE_ALL_DAY="true", OWNER_TZ="Europe/Rome")
    vacation = {
        "id": "v",
        "summary": "Отпуск",
        "start": {"date": "2026-08-10"},
        "end": {"date": "2026-08-12"},
    }
    (window,) = busy_windows([vacation], CAL, settings)

    # Сутки считаются в поясе репетитора: 10 августа 00:00 в Риме — это
    # 9 августа 22:00 UTC. В UTC границы уехали бы на два часа.
    assert window.start_utc == dt.datetime(2026, 8, 9, 22, 0, tzinfo=dt.UTC)
    assert window.end_utc == dt.datetime(2026, 8, 11, 22, 0, tzinfo=dt.UTC)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2026-08-07T17:00:00+02:00", "2026-08-07T17:00:00+02:00"),  # нулевая длина
        ("2026-08-07T17:00:00+02:00", "2026-08-07T16:00:00+02:00"),  # конец раньше начала
    ],
)
def test_degenerate_intervals_dropped(env, start, end):
    """Google такое принимает, а CHECK-констрейнт в базе — нет."""
    settings = env()
    assert busy_windows([event(start=start, end=end)], CAL, settings) == []


def test_ids_are_stable_and_fit_the_column(env):
    """external_id должен помещаться в 128 символов при любом id календаря."""
    settings = env()
    long_calendar = (
        "0b7caa21f0b8658a8e513b6af6c8179ef075e73a25927bc9f5de7039c39080a1"
        "@group.calendar.google.com"
    )

    first = busy_windows([event()], long_calendar, settings)[0].external_id
    second = busy_windows([event()], long_calendar, settings)[0].external_id

    assert first == second
    assert len(first) <= 128


def test_same_event_in_two_calendars_gets_two_ids(env):
    """Один урок может лежать и в основном календаре, и в календаре Preply."""
    settings = env()
    a = busy_windows([event()], "primary", settings)[0].external_id
    b = busy_windows([event()], CAL, settings)[0].external_id
    assert a != b


def test_window_covers_booking_horizon(env):
    settings = env(BOOKING_HORIZON_DAYS="30", BUSY_IMPORT_PAST_DAYS="1")
    now = dt.datetime(2026, 8, 1, 12, 0, tzinfo=dt.UTC)
    since, until = import_window(settings, now)

    assert since == now - dt.timedelta(days=1)
    # С запасом за горизонт: граница окна импорта не должна совпадать
    # с границей того, что бот показывает ученику.
    assert until > now + dt.timedelta(days=30)


def test_missing_summary_still_marks_time_busy(env):
    """Название чужого события нам не важно, важен факт занятости."""
    settings = env()
    (window,) = busy_windows([event(summary=None)], CAL, settings)
    assert window.title == "Занято"
