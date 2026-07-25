"""Как урок из базы превращается в события Google Calendar.

Чистый модуль: ни базы, ни сети, ни времени «сейчас» — на вход урок и настройки,
на выход список событий, которые должны существовать. Благодаря этому правила
раскладки (сколько событий, в какие календари, с какими заголовками) проверяются
обычными тестами, без Postgres и без Google.

Один урок порождает от одного до четырёх событий: сам урок, буфер до, буфер после
и busy-дубль. Поэтому связь урок→событие лежит в отдельной таблице
``calendar_events``, а не колонкой в уроке.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from tutorsync.config import Settings
from tutorsync.db.models import Lesson
from tutorsync.enums import CalendarKey, EventRole, LessonSource

#: Префикс ключей в extendedProperties.private. Свойства видны только этому
#: OAuth-клиенту, поэтому чужое приложение их не затрёт и не прочтёт.
PROP_LESSON_ID = "tutorsync_lesson_id"
PROP_ROLE = "tutorsync_role"
PROP_VERSION = "tutorsync_sync_version"

#: Приписка в описании. Нужна человеку, а не коду: событие, которое сервис
#: перезапишет при следующей сверке, должно само сообщать, что править его руками
#: бесполезно.
MANAGED_NOTE = "Создано tutorsync. Правки вручную будут перезаписаны."


@dataclass(frozen=True)
class PlannedEvent:
    """Событие, которое должно существовать в календаре для данного урока."""

    calendar_key: CalendarKey
    calendar_id: str
    role: EventRole
    start_utc: dt.datetime
    end_utc: dt.datetime
    body: dict[str, Any]


def calendar_key_for(lesson: Lesson) -> CalendarKey:
    """В какой календарь идёт сам урок.

    Уроки с Preply и частные разведены по разным календарям не для красоты:
    в Preply как источник занятости отмечается только календарь частных уроков,
    иначе площадка увидит в нём собственные уроки и посчитает время занятым
    дважды.
    """
    return CalendarKey.PREPLY if lesson.source is LessonSource.PREPLY else CalendarKey.PRIVATE


def _title(lesson: Lesson) -> str:
    name = lesson.student_name_raw or (lesson.student.display_name if lesson.student else None)
    match lesson.source:
        case LessonSource.PREPLY:
            return f"Preply: {name}" if name else "Preply: урок"
        case LessonSource.PRIVATE:
            return f"Урок: {name}" if name else "Урок"
        case LessonSource.BLOCK:
            return lesson.note or "Занято"
    return "Урок"


def _description(lesson: Lesson) -> str:
    lines = [MANAGED_NOTE, f"Источник: {lesson.source.value}"]
    if lesson.note:
        lines.append(f"Заметка: {lesson.note}")
    return "\n".join(lines)


def _timed(start_utc: dt.datetime, end_utc: dt.datetime) -> dict[str, Any]:
    """Время события.

    Всегда UTC с явным timeZone: если отдать Google локальное время без пояса,
    он истолкует его в поясе календаря, и после перевода стрелок урок уедет
    на час — причём только часть уроков, что диагностируется мучительно.
    """
    return {
        "start": {"dateTime": start_utc.astimezone(dt.UTC).isoformat(), "timeZone": "UTC"},
        "end": {"dateTime": end_utc.astimezone(dt.UTC).isoformat(), "timeZone": "UTC"},
    }


def _props(lesson: Lesson, role: EventRole) -> dict[str, Any]:
    return {
        "private": {
            PROP_LESSON_ID: str(lesson.id),
            PROP_ROLE: role.value,
            PROP_VERSION: str(lesson.sync_version),
        }
    }


def _event_body(lesson: Lesson, role: EventRole, summary: str, start: dt.datetime,
                end: dt.datetime) -> dict[str, Any]:
    return {
        "summary": summary,
        "description": _description(lesson),
        # opaque — время считается занятым. transparent означало бы «свободен»,
        # и Preply спокойно поставил бы урок поверх.
        "transparency": "opaque",
        # Напоминания шлёт сам сервис в Telegram; дублировать их всплывашками
        # Google — верный способ, чтобы человек перестал читать и те, и другие.
        "reminders": {"useDefault": False, "overrides": []},
        "extendedProperties": _props(lesson, role),
        **_timed(start, end),
    }


def plan_events(lesson: Lesson, settings: Settings) -> list[PlannedEvent]:
    """Полный набор событий для урока — в том виде, в каком он должен быть в Google.

    Функция описывает желаемое состояние, а не разницу с текущим: сопоставлением
    с уже существующими событиями занимается calendar_sync. Так правила раскладки
    не размазаны по коду синхронизации.
    """
    calendars = settings.managed_calendars
    planned: list[PlannedEvent] = []

    key = calendar_key_for(lesson)
    calendar_id = calendars.get(key.value, "")
    if calendar_id:
        planned.append(
            PlannedEvent(
                calendar_key=key,
                calendar_id=calendar_id,
                role=EventRole.LESSON,
                start_utc=lesson.start_utc,
                end_utc=lesson.end_utc,
                body=_event_body(
                    lesson, EventRole.LESSON, _title(lesson), lesson.start_utc, lesson.end_utc
                ),
            )
        )

    buffers_id = calendars.get(CalendarKey.BUFFERS.value, "")
    if buffers_id:
        # Нулевая длительность буфера — это «буферов нет», а не событие в ноль
        # секунд: Google такое примет и календарь зарастёт пустышками.
        if settings.buffer_before_min > 0:
            start = lesson.start_utc - dt.timedelta(minutes=settings.buffer_before_min)
            planned.append(
                PlannedEvent(
                    calendar_key=CalendarKey.BUFFERS,
                    calendar_id=buffers_id,
                    role=EventRole.BUFFER_BEFORE,
                    start_utc=start,
                    end_utc=lesson.start_utc,
                    body=_event_body(
                        lesson, EventRole.BUFFER_BEFORE, "Буфер до урока", start, lesson.start_utc
                    ),
                )
            )
        if settings.buffer_after_min > 0:
            end = lesson.end_utc + dt.timedelta(minutes=settings.buffer_after_min)
            planned.append(
                PlannedEvent(
                    calendar_key=CalendarKey.BUFFERS,
                    calendar_id=buffers_id,
                    role=EventRole.BUFFER_AFTER,
                    start_utc=lesson.end_utc,
                    end_utc=end,
                    body=_event_body(
                        lesson, EventRole.BUFFER_AFTER, "Буфер после урока", lesson.end_utc, end
                    ),
                )
            )

        # Дубль частного урока в календаре буферов: нужен, только если в Preply
        # неудобно отмечать два календаря как источники занятости и хочется
        # обойтись одним.
        if settings.preply_busy_mirror and lesson.source is not LessonSource.PREPLY:
            planned.append(
                PlannedEvent(
                    calendar_key=CalendarKey.BUFFERS,
                    calendar_id=buffers_id,
                    role=EventRole.BUSY_MIRROR,
                    start_utc=lesson.start_utc,
                    end_utc=lesson.end_utc,
                    body=_event_body(
                        lesson, EventRole.BUSY_MIRROR, "Занято", lesson.start_utc, lesson.end_utc
                    ),
                )
            )

    return planned
