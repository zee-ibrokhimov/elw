"""Общий словарь предметной области.

Лежит в корне пакета и ни от чего не зависит: на эти значения ссылаются и
доменные функции (которые ничего не знают про БД), и модели, и хендлеры бота.
"""

from __future__ import annotations

import enum


class LessonSource(str, enum.Enum):
    """Откуда взялась запись в расписании."""

    PREPLY = "preply"
    PRIVATE = "private"
    #: Служебная занятость, поставленная админом через /block.
    BLOCK = "block"


class LessonStatus(str, enum.Enum):
    SCHEDULED = "scheduled"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class StudentSource(str, enum.Enum):
    TELEGRAM = "telegram"
    PREPLY = "preply"


class IntervalRole(str, enum.Enum):
    """Роль занятого интервала.

    Непересечение проверяется только для LESSON: буферы соседних уроков при
    плотной сетке законно накладываются друг на друга, и констрейнт на них
    ломал бы обычное расписание.
    """

    LESSON = "lesson"
    BUFFER_BEFORE = "buffer_before"
    BUFFER_AFTER = "buffer_after"


class CalendarKey(str, enum.Enum):
    PREPLY = "preply"
    PRIVATE = "private"
    BUFFERS = "buffers"


class EventRole(str, enum.Enum):
    """Роль события в Google Calendar. Пишется в extendedProperties.private."""

    LESSON = "lesson"
    BUFFER_BEFORE = "buffer_before"
    BUFFER_AFTER = "buffer_after"
    #: Дубль урока в календаре Buffers при PREPLY_BUSY_MIRROR=true.
    BUSY_MIRROR = "busy_mirror"


class ManualTaskAction(str, enum.Enum):
    """Что нужно сделать руками на Preply, пока PREPLY_GCAL_LINKED=false."""

    CLOSE_SLOT = "close_slot"
    REOPEN_SLOT = "reopen_slot"


class ManualTaskStatus(str, enum.Enum):
    PENDING = "pending"
    DONE = "done"
    #: Урок отменили раньше, чем задачу успели выполнить — напоминать больше не о чем.
    OBSOLETE = "obsolete"


class ConflictStatus(str, enum.Enum):
    OPEN = "open"
    RESOLVED = "resolved"
    IGNORED = "ignored"


class ConflictResolution(str, enum.Enum):
    KEEP_BOTH = "keep_both"
    CANCELLED_PRIVATE = "cancelled_private"
    HANDLED_MANUALLY = "handled_manually"


class OutboxKind(str, enum.Enum):
    """Внешние эффекты, которые нельзя делать внутри транзакции БД."""

    GCAL_CREATE_EVENT = "gcal_create_event"
    GCAL_PATCH_EVENT = "gcal_patch_event"
    GCAL_DELETE_EVENT = "gcal_delete_event"
    TG_NOTIFY_ADMIN = "tg_notify_admin"
    TG_NOTIFY_STUDENT = "tg_notify_student"


class OutboxStatus(str, enum.Enum):
    PENDING = "pending"
    DONE = "done"
    #: Ретраи исчерпаны — задание ждёт ручного разбора, о нём отправлен алерт.
    FAILED = "failed"


class SyncChannel(str, enum.Enum):
    GCAL = "gcal"
    IMAP = "imap"
    TELEGRAM = "telegram"
    RECONCILE = "reconcile"
    OUTBOX = "outbox"


class SyncResult(str, enum.Enum):
    OK = "ok"
    ERROR = "error"
    SKIPPED = "skipped"


class ExceptionKind(str, enum.Enum):
    """Исключение в рабочем расписании."""

    #: Отпуск, выходной — рабочие часы в эти даты не действуют.
    CLOSED = "closed"
    #: Дополнительное окно сверх обычных часов.
    EXTRA = "extra"
