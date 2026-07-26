"""Откат: убрать всё, что сервис создал, и вернуться к чистому состоянию.

Нужен для тестирования. Пока идёт отладка, в календарь и базу попадают
записи, которые потом надо убрать целиком и без остатка — иначе к моменту
запуска в календаре останется мусор от проб, а вычищать его руками, отличая
тестовые события от настоящих, невозможно.

Два правила, определяющие устройство модуля:

1. **Удаляется только своё.** События опознаются по ``tutorsync_lesson_id``
   в ``extendedProperties.private`` — сервис проставляет его каждому
   созданному событию. Всё, чего там нет, не наше: личные встречи, дни
   рождения, приглашения. Календари из ``EXTRA_BUSY_CALENDAR_IDS`` не
   просматриваются вообще — сервис в них только читает.

2. **Сначала показать, потом делать.** ``build_plan`` ничего не меняет и
   возвращает точный список того, что будет удалено. Выполнение — отдельный
   вызов, который CLI требует подтвердить явным флагом.

Не трогается никогда: ``oauth_credentials`` (иначе заново проходить
авторизацию Google), ``working_hours`` и ``schedule_exceptions`` (рабочее
расписание — настройка, а не результат работы), ``gcal_channels`` (каналы
уведомлений живут на стороне Google, и потеря их идентификаторов в базе
оставила бы висеть подписку, которую уже нечем отозвать).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from tutorsync.config import Settings
from tutorsync.db.models import (
    BusyInterval,
    CalendarEvent,
    ChannelHealth,
    Conflict,
    DeadLetter,
    Lesson,
    ManualTask,
    OutboxJob,
    ProcessedMessage,
    Reminder,
    Student,
    SyncLogEntry,
)
from tutorsync.enums import CalendarKey
from tutorsync.gcal.client import CalendarClient
from tutorsync.gcal.events import PROP_LESSON_ID, PROP_ROLE
from tutorsync.logging import get_logger

log = get_logger(__name__)

#: Порядок важен: сначала то, что ссылается, потом то, на что ссылаются.
#: Каскады в схеме сделали бы часть работы сами, но полагаться на них здесь
#: не стоит — порядок должен быть виден в коде, а не выводиться из схемы.
_TABLES: tuple[tuple[str, type[Any]], ...] = (
    ("sync_log", SyncLogEntry),
    ("dead_letters", DeadLetter),
    ("processed_messages", ProcessedMessage),
    ("outbox", OutboxJob),
    ("reminders", Reminder),
    ("conflicts", Conflict),
    ("manual_tasks", ManualTask),
    ("calendar_events", CalendarEvent),
    ("busy_intervals", BusyInterval),
    ("lessons", Lesson),
    ("channel_health", ChannelHealth),
)


def is_ours(event: dict[str, Any]) -> bool:
    """Событие создано этим сервисом.

    Признак — собственное расширенное свойство. Ни название, ни время, ни
    календарь признаком не являются: в календаре ``Private`` вполне может
    лежать событие, заведённое руками, и удалять его нельзя.
    """
    private = event.get("extendedProperties", {}).get("private", {})
    return bool(private.get(PROP_LESSON_ID))


def describe(event: dict[str, Any]) -> str:
    private = event.get("extendedProperties", {}).get("private", {})
    start = event.get("start", {})
    when = start.get("dateTime") or start.get("date") or "?"
    role = private.get(PROP_ROLE, "?")
    return f"{when} · {event.get('summary', '(без названия)')} · {role}"


@dataclass
class CalendarTarget:
    key: CalendarKey
    calendar_id: str
    events: list[dict[str, Any]] = field(default_factory=list)
    #: Сколько чужих событий в этом календаре оставлено нетронутыми —
    #: показывается в предпросмотре, чтобы было видно, что их не заденет.
    foreign_count: int = 0


@dataclass
class CleanupPlan:
    calendars: list[CalendarTarget] = field(default_factory=list)
    tables: dict[str, int] = field(default_factory=dict)
    #: Календари, которые не удалось просмотреть: (ключ, причина).
    unreachable: list[tuple[str, str]] = field(default_factory=list)
    calendars_skipped_reason: str | None = None

    @property
    def event_count(self) -> int:
        return sum(len(target.events) for target in self.calendars)

    @property
    def row_count(self) -> int:
        return sum(self.tables.values())

    @property
    def is_empty(self) -> bool:
        return self.event_count == 0 and self.row_count == 0


@dataclass
class CleanupReport:
    events_deleted: int = 0
    events_failed: list[tuple[str, str]] = field(default_factory=list)
    rows_deleted: dict[str, int] = field(default_factory=dict)


async def build_plan(
    session: AsyncSession,
    client: CalendarClient | None,
    settings: Settings,
    *,
    now: dt.datetime,
    days: int = 365,
    with_students: bool = False,
) -> CleanupPlan:
    """Собирает точный список к удалению. Ничего не меняет."""
    plan = CleanupPlan()

    tables = list(_TABLES)
    if with_students:
        tables.append(("students", Student))
    for name, model in tables:
        count = await session.scalar(sa.select(sa.func.count()).select_from(model))
        plan.tables[name] = int(count or 0)

    if client is None:
        plan.calendars_skipped_reason = (
            "Google-аккаунт не подключён — события в календарях не просматривались"
        )
        return plan

    window_from = now - dt.timedelta(days=days)
    window_to = now + dt.timedelta(days=days)

    for key_name, calendar_id in settings.managed_calendars.items():
        key = CalendarKey(key_name)
        if not calendar_id:
            plan.unreachable.append((key_name, "не задан calendar id"))
            continue
        try:
            events = await client.list_events(calendar_id, window_from, window_to)
        # Один недоступный календарь не должен прятать содержимое остальных:
        # план всё равно должен быть показан целиком, с пометкой о пропуске.
        except Exception as exc:
            plan.unreachable.append((key_name, str(exc)))
            continue

        target = CalendarTarget(key=key, calendar_id=calendar_id)
        for event in events:
            if is_ours(event):
                target.events.append(event)
            else:
                target.foreign_count += 1
        plan.calendars.append(target)

    return plan


async def apply_plan(
    session: AsyncSession,
    client: CalendarClient | None,
    plan: CleanupPlan,
    *,
    with_students: bool = False,
) -> CleanupReport:
    """Выполняет план: сначала календари, потом база.

    Порядок именно такой. Ссылки на события живут в ``calendar_events``, и если
    сначала очистить базу, до непонравившегося события в календаре уже не
    добраться — оно останется висеть навсегда.
    """
    report = CleanupReport()

    if client is not None:
        for target in plan.calendars:
            for event in target.events:
                event_id = event.get("id", "")
                try:
                    await client.delete_event(target.calendar_id, event_id)
                    report.events_deleted += 1
                except Exception as exc:
                    report.events_failed.append((event_id, str(exc)))
                    log.error(
                        "cleanup.delete_failed",
                        calendar=target.key.value,
                        event_id=event_id,
                        error=str(exc),
                    )

    tables = list(_TABLES)
    if with_students:
        tables.append(("students", Student))
    for name, model in tables:
        result = await session.execute(sa.delete(model))
        report.rows_deleted[name] = int(result.rowcount or 0)

    log.info(
        "cleanup.done",
        events_deleted=report.events_deleted,
        events_failed=len(report.events_failed),
        rows_deleted=sum(report.rows_deleted.values()),
    )
    return report


def render_plan(plan: CleanupPlan, *, with_students: bool) -> str:
    """Человекочитаемый предпросмотр для вывода в консоль."""
    lines: list[str] = []

    lines.append("События в календарях:")
    if plan.calendars_skipped_reason:
        lines.append(f"  {plan.calendars_skipped_reason}")
    for target in plan.calendars:
        lines.append(f"  {target.key.value}: удалить {len(target.events)}, "
                     f"чужих не тронуто {target.foreign_count}")
        for event in target.events[:10]:
            lines.append(f"      {describe(event)}")
        if len(target.events) > 10:
            lines.append(f"      ... и ещё {len(target.events) - 10}")
    for key_name, reason in plan.unreachable:
        lines.append(f"  {key_name}: пропущен — {reason}")

    lines.append("")
    lines.append("Строки в базе:")
    for name, count in plan.tables.items():
        if count:
            lines.append(f"  {name}: {count}")
    if not plan.row_count:
        lines.append("  пусто")

    lines.append("")
    lines.append("Не затрагивается: oauth_credentials, working_hours, "
                 "schedule_exceptions, gcal_channels")
    if not with_students:
        lines.append("Не затрагивается: students (сбросить — флаг --with-students)")

    return "\n".join(lines)
