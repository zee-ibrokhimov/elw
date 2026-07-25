"""Импорт занятости из календарей, которые сервис только читает.

Зачем это нужно раньше разбора писем. Уроки с Preply уже лежат в календаре,
который ведёт сама площадка, а частные ученики — в основном календаре
репетитора. Обе эти занятости надо знать, чтобы бот не предложил ученику время,
которое на самом деле занято, — и обе доступны без единого распарсенного письма.

Занятость материализуется в базу, а не запрашивается у Google в момент показа
слотов. Иначе бронирование перестаёт работать каждый раз, когда недоступен
Google, и каждый показ расписания превращается в поход наружу. Расплата —
окно устаревания в один интервал опроса; на фоне того, что Preply сам узнаёт
об изменениях с задержкой в минуты, это ничего не меняет.

Разбор событий вынесен в чистую функцию: правила «что считать занятым» —
единственное место, где ошибка тихо приводит к двойному бронированию, и
проверять их нужно тестами, а не наблюдением за проданными слотами.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from tutorsync.config import Settings, get_settings
from tutorsync.db.models import BusyInterval, Lesson
from tutorsync.enums import IntervalRole, LessonSource, LessonStatus
from tutorsync.gcal.client import CalendarClient
from tutorsync.gcal.events import PROP_LESSON_ID
from tutorsync.logging import get_logger

log = get_logger(__name__)

#: Максимальная длина external_id в схеме — 128 символов, а пара
#: «id календаря + id события» у Google легко перебирает этот лимит: один
#: только id календаря Preply занимает 90. Поэтому ключом служит хеш пары.
#: Обратно он не разворачивается, но и не нужен: в чужие календари сервис
#: никогда не пишет, сопоставление требуется только само с собой.
_ID_PREFIX = "ext:"


@dataclass(frozen=True)
class BusyWindow:
    """Занятый интервал, вытащенный из чужого события."""

    external_id: str
    calendar_id: str
    title: str
    start_utc: dt.datetime
    end_utc: dt.datetime


def _external_id(calendar_id: str, event_id: str) -> str:
    digest = hashlib.sha256(f"{calendar_id}\x00{event_id}".encode()).hexdigest()
    return f"{_ID_PREFIX}{digest[:48]}"


def _parse_dt(value: dict[str, Any]) -> dt.datetime | None:
    """Разбирает start/end события. Возвращает None для событий на весь день."""
    raw = value.get("dateTime")
    if not raw:
        return None
    parsed = dt.datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        # Google всегда отдаёт смещение, но naive-значение в базу не пройдёт,
        # а «поправить» его тут молча — значит угадать пояс.
        return None
    return parsed.astimezone(dt.UTC)


def busy_windows(
    events: list[dict[str, Any]], calendar_id: str, settings: Settings
) -> list[BusyWindow]:
    """Отбирает из событий календаря те, что действительно занимают время.

    Отсеиваются четыре вида событий, и каждый — по своей причине:

    * ``transparency=transparent`` — владелец календаря сам пометил, что
      свободен. Уважать эту пометку дешевле, чем объяснять потом, почему бот
      не отдаёт время, помеченное как свободное.
    * ``status=cancelled`` — отменённые вхождения повторяющихся серий Google
      продолжает отдавать; считать их занятостью значит держать вечно занятым
      время отменённого урока.
    * события на весь день — они почти всегда не про занятость (дни рождения,
      праздники, «в отпуске»). Отпуск закрывается через schedule_exceptions,
      где у него есть явные границы. Поведение переключается настройкой.
    * собственные события сервиса — если управляемый календарь по недосмотру
      попал в список читаемых, урок посчитался бы занятым дважды.
    """
    windows: list[BusyWindow] = []
    for event in events:
        if event.get("status") == "cancelled":
            continue
        if event.get("transparency") == "transparent":
            continue
        props = (event.get("extendedProperties") or {}).get("private") or {}
        if PROP_LESSON_ID in props:
            continue

        start = _parse_dt(event.get("start") or {})
        end = _parse_dt(event.get("end") or {})
        if start is None or end is None:
            if settings.busy_include_all_day and (event.get("start") or {}).get("date"):
                start, end = _all_day_bounds(event, settings)
            else:
                continue
        if start is None or end is None or end <= start:
            # Событий нулевой и отрицательной длины быть не должно, но Google
            # их принимает, а CHECK-констрейнт в базе — нет.
            continue

        windows.append(
            BusyWindow(
                external_id=_external_id(calendar_id, str(event.get("id", ""))),
                calendar_id=calendar_id,
                title=str(event.get("summary") or "Занято")[:128],
                start_utc=start,
                end_utc=end,
            )
        )
    return windows


def _all_day_bounds(
    event: dict[str, Any], settings: Settings
) -> tuple[dt.datetime | None, dt.datetime | None]:
    """Границы события на весь день в поясе репетитора.

    Даты у Google календарные и пояса не несут: «10 августа» — это сутки в том
    поясе, где человек живёт, а не в UTC. Считать их в UTC значило бы смещать
    границы занятости на несколько часов.
    """
    try:
        start_date = dt.date.fromisoformat((event.get("start") or {})["date"])
        end_date = dt.date.fromisoformat((event.get("end") or {})["date"])
    except (KeyError, ValueError):
        return None, None
    zone = settings.owner_zone
    start = dt.datetime.combine(start_date, dt.time.min, tzinfo=zone)
    end = dt.datetime.combine(end_date, dt.time.min, tzinfo=zone)
    return start.astimezone(dt.UTC), end.astimezone(dt.UTC)


def import_window(settings: Settings, now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    """Отрезок времени, за который тянется занятость.

    Назад — на сутки: слоты в прошлом никому не нужны, но урок, начавшийся
    вчера вечером и идущий за полночь, обязан быть виден. Вперёд — на горизонт
    бронирования с запасом, чтобы граница окна не совпадала с границей того,
    что бот показывает.
    """
    return (
        now - dt.timedelta(days=settings.busy_import_past_days),
        now + dt.timedelta(days=settings.booking_horizon_days + 7),
    )


async def import_busy(
    session: AsyncSession, client: CalendarClient, *, now: dt.datetime | None = None
) -> dict[str, int]:
    """Приводит импортированную занятость в соответствие с чужими календарями.

    Как и синхронизация календаря, это сравнение желаемого с текущим, а не
    накат изменений: событие могли отменить, подвинуть или удалить, а узнать
    об этом можно только по отсутствию его в свежей выборке.
    """
    settings = get_settings()
    now = now or dt.datetime.now(dt.UTC)
    since, until = import_window(settings, now)

    managed = {value for value in settings.managed_calendars.values() if value}
    stats = {"seen": 0, "created": 0, "updated": 0, "removed": 0, "calendars": 0}

    fresh: dict[str, BusyWindow] = {}
    for calendar_id in settings.extra_busy_calendars:
        if calendar_id in managed:
            # Управляемый календарь в списке читаемых — занятость удвоится.
            log.warning("busy.skip_managed_calendar", calendar_id=calendar_id)
            continue
        events = await client.list_events(calendar_id, since, until)
        stats["calendars"] += 1
        for window in busy_windows(events, calendar_id, settings):
            # Один и тот же урок может лежать в двух читаемых календарях;
            # ключ по паре (календарь, событие) их различает, а совпадение
            # интервалов ничему не мешает — роль external вне EXCLUDE.
            fresh[window.external_id] = window
    stats["seen"] = len(fresh)

    existing = {
        row.external_id: row
        for row in (
            await session.execute(
                sa.select(Lesson).where(
                    Lesson.source == LessonSource.EXTERNAL,
                    Lesson.start_utc >= since,
                    Lesson.start_utc < until,
                )
            )
        )
        .scalars()
        .all()
        if row.external_id
    }

    for external_id, window in fresh.items():
        row = existing.pop(external_id, None)
        if row is None:
            session.add(_new_lesson(window))
            stats["created"] += 1
            continue
        if row.start_utc != window.start_utc or row.end_utc != window.end_utc:
            row.start_utc = window.start_utc
            row.end_utc = window.end_utc
            row.duration_min = _minutes(window)
            row.student_name_raw = window.title
            await _rewrite_interval(session, row)
            stats["updated"] += 1
        elif row.student_name_raw != window.title:
            row.student_name_raw = window.title
            stats["updated"] += 1

    # Осталось то, чего в календарях больше нет. Удаляем, а не гасим флагом:
    # это не наша бронь, истории по ней вести незачем, а живой мусор в
    # busy_intervals навсегда закрыл бы освободившееся время.
    for row in existing.values():
        await session.delete(row)
        stats["removed"] += 1

    log.info("busy.imported", **stats)
    return stats


def _minutes(window: BusyWindow) -> int:
    return max(int((window.end_utc - window.start_utc).total_seconds() // 60), 1)


def _new_lesson(window: BusyWindow) -> Lesson:
    lesson = Lesson(
        source=LessonSource.EXTERNAL,
        external_id=window.external_id,
        student_name_raw=window.title,
        start_utc=window.start_utc,
        end_utc=window.end_utc,
        duration_min=_minutes(window),
        status=LessonStatus.SCHEDULED,
        sync_version=1,
        conflict_accepted=False,
    )
    lesson.intervals.append(
        BusyInterval(
            role=IntervalRole.EXTERNAL,
            start_utc=window.start_utc,
            end_utc=window.end_utc,
            active=True,
            conflict_accepted=False,
        )
    )
    return lesson


async def _rewrite_interval(session: AsyncSession, lesson: Lesson) -> None:
    await session.execute(
        sa.delete(BusyInterval).where(BusyInterval.lesson_id == lesson.id)
    )
    session.add(
        BusyInterval(
            lesson_id=lesson.id,
            role=IntervalRole.EXTERNAL,
            start_utc=lesson.start_utc,
            end_utc=lesson.end_utc,
            active=True,
            conflict_accepted=False,
        )
    )
