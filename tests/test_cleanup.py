"""Тесты отката.

Здесь проверяется ровно одно свойство, но оно того стоит: команда сброса не
должна трогать ничего, кроме созданного сервисом. Ошибка в фильтре удалит
из календаря настоящие встречи, и восстановить их будет нечем — Google не
хранит корзину для удалённых через API событий.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tutorsync.db.base import Base
from tutorsync.gcal.events import PROP_LESSON_ID, PROP_ROLE
from tutorsync.services.cleanup import apply_plan, build_plan, is_ours

NOW = dt.datetime(2026, 7, 26, 12, 0, tzinfo=dt.UTC)

CAL_PREPLY = "preply@group.calendar.google.com"
CAL_PRIVATE = "private@group.calendar.google.com"
CAL_BUFFERS = "buffers@group.calendar.google.com"


def ours(event_id: str, lesson_id: int = 1, role: str = "lesson") -> dict[str, Any]:
    return {
        "id": event_id,
        "summary": "Урок",
        "start": {"dateTime": "2026-07-27T10:00:00Z"},
        "extendedProperties": {
            "private": {PROP_LESSON_ID: str(lesson_id), PROP_ROLE: role}
        },
    }


def foreign(event_id: str, summary: str = "Врач") -> dict[str, Any]:
    return {
        "id": event_id,
        "summary": summary,
        "start": {"dateTime": "2026-07-27T14:00:00Z"},
    }


class StubClient:
    def __init__(
        self,
        events: dict[str, list[dict[str, Any]]],
        failing: frozenset[str] = frozenset(),
    ) -> None:
        self._events = events
        self._failing = failing
        self.deleted: list[tuple[str, str]] = []

    async def list_events(
        self, calendar_id: str, time_min: dt.datetime, time_max: dt.datetime, **_: Any
    ) -> list[dict[str, Any]]:
        if calendar_id in self._failing:
            raise RuntimeError("календарь недоступен")
        return self._events.get(calendar_id, [])

    async def delete_event(self, calendar_id: str, event_id: str) -> None:
        self.deleted.append((calendar_id, event_id))


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
        GCAL_CALENDAR_PREPLY=CAL_PREPLY,
        GCAL_CALENDAR_PRIVATE=CAL_PRIVATE,
        GCAL_CALENDAR_BUFFERS=CAL_BUFFERS,
    )


def test_is_ours_requires_service_marker():
    assert is_ours(ours("e1"))
    assert not is_ours(foreign("e2"))
    # Пустое свойство — не признак: так выглядит событие, у которого свойства
    # затёрли вручную, и удалять его наугад нельзя.
    assert not is_ours({"id": "e3", "extendedProperties": {"private": {PROP_LESSON_ID: ""}}})
    assert not is_ours({"id": "e4", "extendedProperties": {}})


async def test_plan_separates_foreign_events(session, settings):
    client = StubClient({
        CAL_PRIVATE: [ours("a"), foreign("b"), ours("c", role="buffer_before")],
        CAL_PREPLY: [foreign("d"), foreign("e")],
        CAL_BUFFERS: [],
    })

    plan = await build_plan(session, client, settings, now=NOW)

    by_key = {target.key.value: target for target in plan.calendars}
    assert [e["id"] for e in by_key["private"].events] == ["a", "c"]
    assert by_key["private"].foreign_count == 1
    # В календаре Preply нет ни одного нашего события — значит удалять нечего,
    # хотя события там есть.
    assert by_key["preply"].events == []
    assert by_key["preply"].foreign_count == 2
    assert plan.event_count == 2


async def test_unreachable_calendar_does_not_hide_the_rest(session, settings):
    client = StubClient(
        {CAL_PRIVATE: [ours("a")], CAL_BUFFERS: [ours("b")]},
        failing=frozenset({CAL_PREPLY}),
    )

    plan = await build_plan(session, client, settings, now=NOW)

    assert plan.event_count == 2
    assert [key for key, _ in plan.unreachable] == ["preply"]


async def test_apply_deletes_only_planned_events(session, settings):
    client = StubClient({
        CAL_PRIVATE: [ours("a"), foreign("keep-me"), ours("c")],
        CAL_PREPLY: [foreign("keep-me-too")],
        CAL_BUFFERS: [],
    })
    plan = await build_plan(session, client, settings, now=NOW)

    report = await apply_plan(session, client, plan)

    assert report.events_deleted == 2
    assert sorted(event_id for _, event_id in client.deleted) == ["a", "c"]


async def test_missing_credentials_still_allows_db_cleanup(session, settings):
    plan = await build_plan(session, None, settings, now=NOW)

    assert plan.calendars == []
    assert plan.calendars_skipped_reason is not None
    # Таблицы всё равно посчитаны — сброс до подключения Google это рабочий случай.
    assert "lessons" in plan.tables


async def test_students_untouched_unless_asked(session, settings):
    client = StubClient({})

    assert "students" not in (await build_plan(session, client, settings, now=NOW)).tables

    with_students = await build_plan(session, client, settings, now=NOW, with_students=True)
    assert "students" in with_students.tables
