"""Схема базы.

Источник истины по расписанию — эта база, Google Calendar является её проекцией.
При расхождении побеждает то, что уже подтверждено ученику.

Ключевое место схемы — таблица ``busy_intervals``: именно на ней в Postgres висит
EXCLUDE-констрейнт, запрещающий пересечение уроков. Сам констрейнт объявлен не
здесь, а в миграции (``migrations/versions/0001_initial.py``), потому что
выражается диалектом Postgres и не имеет аналога в SQLite — см. репозитории в
``tutorsync/db/repo/``, где различие спрятано за общим интерфейсом.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tutorsync.db.base import Base, JsonType, TimestampMixin, enum_column
from tutorsync.db.types import UtcDateTime
from tutorsync.enums import (
    CalendarKey,
    ConflictResolution,
    ConflictStatus,
    EventRole,
    ExceptionKind,
    IntervalRole,
    LessonSource,
    LessonStatus,
    ManualTaskAction,
    ManualTaskStatus,
    OutboxKind,
    OutboxStatus,
    StudentSource,
    SyncChannel,
    SyncResult,
)

# ==============================================================================
#  Люди и расписание
# ==============================================================================


class Student(Base, TimestampMixin):
    __tablename__ = "students"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: NULL у учеников с Preply: они существуют только как имя из письма.
    tg_user_id: Mapped[int | None] = mapped_column(sa.BigInteger, unique=True)
    tg_chat_id: Mapped[int | None] = mapped_column(sa.BigInteger)
    display_name: Mapped[str] = mapped_column(sa.String(128))
    language: Mapped[str] = mapped_column(sa.String(8), default="ru")
    #: IANA-имя пояса ученика; слоты показываются в нём, хранится всё в UTC.
    tz: Mapped[str] = mapped_column(sa.String(64), default="Europe/Rome")
    source: Mapped[StudentSource] = mapped_column(enum_column(StudentSource))
    consent_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    #: Заполняется /delete_me: персональные данные стёрты, история обезличена.
    anonymized_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)

    lessons: Mapped[list[Lesson]] = relationship(back_populates="student")


class Lesson(Base, TimestampMixin):
    __tablename__ = "lessons"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[LessonSource] = mapped_column(enum_column(LessonSource))
    #: Идентификатор на стороне источника. Для Preply — из письма, а если письмо
    #: своего ID не содержит, синтезируется как хеш (тип, имя, время начала).
    external_id: Mapped[str | None] = mapped_column(sa.String(128))

    student_id: Mapped[int | None] = mapped_column(sa.ForeignKey("students.id", ondelete="SET NULL"))
    #: Имя как оно пришло из письма Preply — там нет ничего, кроме имени.
    student_name_raw: Mapped[str | None] = mapped_column(sa.String(128))

    start_utc: Mapped[dt.datetime] = mapped_column(UtcDateTime)
    end_utc: Mapped[dt.datetime] = mapped_column(UtcDateTime)
    duration_min: Mapped[int] = mapped_column(sa.Integer)

    status: Mapped[LessonStatus] = mapped_column(
        enum_column(LessonStatus), default=LessonStatus.SCHEDULED
    )
    #: Пояс, в котором ученик видел время при бронировании. Нужен, чтобы после
    #: перевода стрелок писать ему то же самое локальное время, что он подтверждал.
    booked_tz: Mapped[str | None] = mapped_column(sa.String(64))

    #: Перенос = отмена + новая бронь; здесь хранится связь с отменённой записью.
    replaces_lesson_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("lessons.id", ondelete="SET NULL")
    )
    cancelled_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    cancel_reason: Mapped[str | None] = mapped_column(sa.String(256))
    #: Свободный текст для /block («врач», «отпуск»).
    note: Mapped[str | None] = mapped_column(sa.String(256))

    #: Урок сохранён, несмотря на пересечение с другим. Ставится только для
    #: оплаченных уроков с Preply: не принять их в базу нельзя, а автоотмена
    #: чего бы то ни было недопустима — вместо неё заводится запись в conflicts.
    conflict_accepted: Mapped[bool] = mapped_column(sa.Boolean, default=False)

    #: Инкрементируется при каждом изменении, уезжает в extendedProperties
    #: события Google. По нему видно, что событие в календаре отстало.
    sync_version: Mapped[int] = mapped_column(sa.Integer, default=1)

    student: Mapped[Student | None] = relationship(back_populates="lessons")
    intervals: Mapped[list[BusyInterval]] = relationship(
        back_populates="lesson", cascade="all, delete-orphan"
    )
    calendar_events: Mapped[list[CalendarEvent]] = relationship(
        back_populates="lesson", cascade="all, delete-orphan"
    )

    __table_args__ = (
        sa.CheckConstraint("end_utc > start_utc", name="end_after_start"),
        sa.CheckConstraint("duration_min > 0", name="positive_duration"),
        sa.Index("ix_lessons_start_utc", "start_utc"),
        sa.Index("ix_lessons_status_start", "status", "start_utc"),
        # Идемпотентность №1: повторная доставка того же письма Preply
        # или того же вебхука не создаёт вторую запись.
        sa.Index(
            "uq_lessons_source_external_id",
            "source",
            "external_id",
            unique=True,
            postgresql_where=sa.text("external_id IS NOT NULL"),
            sqlite_where=sa.text("external_id IS NOT NULL"),
        ),
        # Идемпотентность №2: один ученик не может дважды занять одно и то же
        # время — страхует на случай, если external_id по какой-то причине разный.
        sa.Index(
            "uq_lessons_student_start",
            "student_id",
            "start_utc",
            unique=True,
            postgresql_where=sa.text("status = 'scheduled' AND student_id IS NOT NULL"),
            sqlite_where=sa.text("status = 'scheduled' AND student_id IS NOT NULL"),
        ),
    )


class BusyInterval(Base):
    """Материализованная занятость: сам урок и его буферы — отдельными строками.

    Расчёт свободных слотов читает только эту таблицу, поэтому не должен знать
    ничего про буферы, блоки и источники.
    """

    __tablename__ = "busy_intervals"

    id: Mapped[int] = mapped_column(primary_key=True)
    lesson_id: Mapped[int] = mapped_column(sa.ForeignKey("lessons.id", ondelete="CASCADE"))
    role: Mapped[IntervalRole] = mapped_column(enum_column(IntervalRole))
    start_utc: Mapped[dt.datetime] = mapped_column(UtcDateTime)
    end_utc: Mapped[dt.datetime] = mapped_column(UtcDateTime)
    #: Снимается при отмене вместо удаления — история остаётся разбираемой.
    active: Mapped[bool] = mapped_column(sa.Boolean, default=True)
    #: Дублируется с урока: попадает в предикат EXCLUDE-констрейнта, а тот
    #: не умеет заглядывать в соседнюю таблицу.
    conflict_accepted: Mapped[bool] = mapped_column(sa.Boolean, default=False)

    lesson: Mapped[Lesson] = relationship(back_populates="intervals")

    __table_args__ = (
        sa.CheckConstraint("end_utc > start_utc", name="end_after_start"),
        sa.Index("ix_busy_intervals_range", "start_utc", "end_utc"),
        sa.Index("ix_busy_intervals_lesson_id", "lesson_id"),
        # EXCLUDE USING gist по пересечению tstzrange — только для Postgres,
        # добавляется в миграции 0001. На SQLite ту же инвариантность
        # обеспечивает SqliteBusyRepository через BEGIN IMMEDIATE.
    )


# ==============================================================================
#  Google Calendar
# ==============================================================================


class CalendarEvent(Base, TimestampMixin):
    """Связь урока с конкретными событиями в календарях.

    Хранится отдельно от урока, потому что один урок порождает до четырёх
    событий: сам урок, буфер до, буфер после и (опционально) busy-дубль.
    """

    __tablename__ = "calendar_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    lesson_id: Mapped[int] = mapped_column(sa.ForeignKey("lessons.id", ondelete="CASCADE"))
    calendar_key: Mapped[CalendarKey] = mapped_column(enum_column(CalendarKey))
    gcal_calendar_id: Mapped[str] = mapped_column(sa.String(256))
    gcal_event_id: Mapped[str] = mapped_column(sa.String(256))
    role: Mapped[EventRole] = mapped_column(enum_column(EventRole))
    #: Версия урока, отражённая в этом событии. Меньше, чем lessons.sync_version,
    #: означает, что событие ещё не догнало базу.
    sync_version: Mapped[int] = mapped_column(sa.Integer, default=1)
    etag: Mapped[str | None] = mapped_column(sa.String(128))
    last_synced_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)

    lesson: Mapped[Lesson] = relationship(back_populates="calendar_events")

    __table_args__ = (
        sa.UniqueConstraint("gcal_calendar_id", "gcal_event_id", name="uq_calendar_event"),
        sa.Index("ix_calendar_events_lesson_id", "lesson_id"),
    )


class OAuthCredential(Base, TimestampMixin):
    __tablename__ = "oauth_credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_email: Mapped[str] = mapped_column(sa.String(256), unique=True)
    #: Fernet-шифртекст. Ключ — SECRET_ENC_KEY, в базе его нет.
    refresh_token_enc: Mapped[str] = mapped_column(sa.Text)
    access_token_enc: Mapped[str | None] = mapped_column(sa.Text)
    token_expiry: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    scopes: Mapped[str] = mapped_column(sa.Text, default="")


class GCalChannel(Base, TimestampMixin):
    """Канал events.watch и точка инкрементальной синхронизации по одному календарю."""

    __tablename__ = "gcal_channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    calendar_id: Mapped[str] = mapped_column(sa.String(256), unique=True)
    channel_id: Mapped[str | None] = mapped_column(sa.String(128))
    resource_id: Mapped[str | None] = mapped_column(sa.String(256))
    #: Отдаётся Google и возвращается в X-Goog-Channel-Token — так вебхук
    #: отличает настоящее уведомление от чужого запроса на угаданный путь.
    token: Mapped[str | None] = mapped_column(sa.String(128))
    #: Каналы живут ~7 дней; cron продлевает заранее.
    expiration: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    sync_token: Mapped[str | None] = mapped_column(sa.Text)
    last_sync_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)


# ==============================================================================
#  Ручные действия и конфликты
# ==============================================================================


class ManualTask(Base, TimestampMixin):
    """Очередь «сделай руками на Preply» — работает, пока PREPLY_GCAL_LINKED=false."""

    __tablename__ = "manual_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    lesson_id: Mapped[int] = mapped_column(sa.ForeignKey("lessons.id", ondelete="CASCADE"))
    action: Mapped[ManualTaskAction] = mapped_column(enum_column(ManualTaskAction))
    status: Mapped[ManualTaskStatus] = mapped_column(
        enum_column(ManualTaskStatus), default=ManualTaskStatus.PENDING
    )
    tg_chat_id: Mapped[int | None] = mapped_column(sa.BigInteger)
    #: Сообщение с карточкой — по нему кнопка «Сделал» заменяется на отметку.
    tg_message_id: Mapped[int | None] = mapped_column(sa.BigInteger)
    reminders_sent: Mapped[int] = mapped_column(sa.Integer, default=0)
    last_reminded_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    done_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    done_by_tg_id: Mapped[int | None] = mapped_column(sa.BigInteger)

    lesson: Mapped[Lesson] = relationship()

    __table_args__ = (
        sa.Index(
            "uq_manual_tasks_lesson_action_pending",
            "lesson_id",
            "action",
            unique=True,
            postgresql_where=sa.text("status = 'pending'"),
            sqlite_where=sa.text("status = 'pending'"),
        ),
        sa.Index("ix_manual_tasks_status", "status"),
    )


class Conflict(Base, TimestampMixin):
    """Обнаруженное постфактум пересечение двух броней.

    Заводится, а не разрешается автоматически: типичная причина — ученик с Preply
    успел занять время в окне задержки синхронизации, и его урок уже оплачен.
    """

    __tablename__ = "conflicts"

    id: Mapped[int] = mapped_column(primary_key=True)
    lesson_a_id: Mapped[int] = mapped_column(sa.ForeignKey("lessons.id", ondelete="CASCADE"))
    lesson_b_id: Mapped[int] = mapped_column(sa.ForeignKey("lessons.id", ondelete="CASCADE"))
    status: Mapped[ConflictStatus] = mapped_column(
        enum_column(ConflictStatus), default=ConflictStatus.OPEN
    )
    resolution: Mapped[ConflictResolution | None] = mapped_column(enum_column(ConflictResolution))
    resolved_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    resolved_by_tg_id: Mapped[int | None] = mapped_column(sa.BigInteger)

    __table_args__ = (
        sa.UniqueConstraint("lesson_a_id", "lesson_b_id", name="uq_conflict_pair"),
        sa.CheckConstraint("lesson_a_id <> lesson_b_id", name="distinct_lessons"),
    )


# ==============================================================================
#  Рабочее расписание
# ==============================================================================


class WorkingHours(Base, TimestampMixin):
    """Обычные рабочие часы по дням недели, в поясе репетитора (OWNER_TZ)."""

    __tablename__ = "working_hours"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: 0 — понедельник, 6 — воскресенье (совпадает с date.weekday()).
    weekday: Mapped[int] = mapped_column(sa.SmallInteger)
    start_local: Mapped[dt.time] = mapped_column(sa.Time)
    end_local: Mapped[dt.time] = mapped_column(sa.Time)
    active: Mapped[bool] = mapped_column(sa.Boolean, default=True)

    __table_args__ = (
        sa.CheckConstraint("weekday BETWEEN 0 AND 6", name="weekday_range"),
        sa.Index("ix_working_hours_weekday", "weekday"),
    )


class ScheduleException(Base, TimestampMixin):
    """Отпуска и разовые окна. Даты — календарные, в поясе репетитора."""

    __tablename__ = "schedule_exceptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    date_from: Mapped[dt.date] = mapped_column(sa.Date)
    date_to: Mapped[dt.date] = mapped_column(sa.Date)
    kind: Mapped[ExceptionKind] = mapped_column(enum_column(ExceptionKind))
    #: Для CLOSED пусто — закрыт весь день. Для EXTRA задаёт границы окна.
    start_local: Mapped[dt.time | None] = mapped_column(sa.Time)
    end_local: Mapped[dt.time | None] = mapped_column(sa.Time)
    note: Mapped[str | None] = mapped_column(sa.String(256))

    __table_args__ = (
        sa.CheckConstraint("date_to >= date_from", name="date_range"),
        sa.Index("ix_schedule_exceptions_dates", "date_from", "date_to"),
    )


# ==============================================================================
#  Доставка эффектов, приём писем, наблюдаемость
# ==============================================================================


class OutboxJob(Base):
    """Внешние вызовы, поставленные в очередь в той же транзакции, что и запись в БД.

    Смысл: коммит расписания и обращение к Google или Telegram не могут быть
    атомарными, поэтому наружу не ходим внутри транзакции вообще. Воркер
    разбирает эту таблицу через SELECT ... FOR UPDATE SKIP LOCKED, так что
    ни Redis, ни Celery для этого не нужны.
    """

    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[OutboxKind] = mapped_column(enum_column(OutboxKind))
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType)
    status: Mapped[OutboxStatus] = mapped_column(
        enum_column(OutboxStatus), default=OutboxStatus.PENDING
    )
    run_after: Mapped[dt.datetime] = mapped_column(UtcDateTime)
    attempts: Mapped[int] = mapped_column(sa.Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    trace_id: Mapped[str | None] = mapped_column(sa.String(32))
    #: Ключ склейки: повторная постановка того же эффекта не создаёт дубль.
    dedup_key: Mapped[str | None] = mapped_column(sa.String(200))
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, server_default=sa.func.now())
    done_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)

    __table_args__ = (
        sa.Index("ix_outbox_ready", "status", "run_after"),
        sa.Index(
            "uq_outbox_dedup_key",
            "dedup_key",
            unique=True,
            postgresql_where=sa.text("dedup_key IS NOT NULL AND status = 'pending'"),
            sqlite_where=sa.text("dedup_key IS NOT NULL AND status = 'pending'"),
        ),
    )


class ProcessedMessage(Base):
    """Идемпотентность приёма писем: IMAP умеет доставить одно письмо дважды."""

    __tablename__ = "processed_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[str] = mapped_column(sa.String(512), unique=True)
    imap_uid: Mapped[int | None] = mapped_column(sa.BigInteger)
    parser: Mapped[str | None] = mapped_column(sa.String(64))
    processed_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, server_default=sa.func.now())


class DeadLetter(Base):
    """Письмо, которое не разобрал ни один парсер.

    Не проглатывается: тело целиком лежит здесь, а админу уходит алерт. Формат
    писем Preply меняется без предупреждения, и это единственный способ узнать
    об этом раньше, чем ученик придёт на урок, которого нет в расписании.
    """

    __tablename__ = "dead_letters"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(sa.String(32), default="imap")
    message_id: Mapped[str | None] = mapped_column(sa.String(512))
    sender: Mapped[str | None] = mapped_column(sa.String(256))
    subject: Mapped[str | None] = mapped_column(sa.String(512))
    raw: Mapped[str] = mapped_column(sa.Text)
    error: Mapped[str | None] = mapped_column(sa.Text)
    trace_id: Mapped[str | None] = mapped_column(sa.String(32))
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, server_default=sa.func.now())
    resolved_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)


class SyncLogEntry(Base):
    __tablename__ = "sync_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[dt.datetime] = mapped_column(UtcDateTime, server_default=sa.func.now())
    trace_id: Mapped[str | None] = mapped_column(sa.String(32))
    channel: Mapped[SyncChannel] = mapped_column(enum_column(SyncChannel))
    action: Mapped[str] = mapped_column(sa.String(64))
    entity_type: Mapped[str | None] = mapped_column(sa.String(64))
    entity_id: Mapped[str | None] = mapped_column(sa.String(128))
    result: Mapped[SyncResult] = mapped_column(enum_column(SyncResult))
    error: Mapped[str | None] = mapped_column(sa.Text)
    duration_ms: Mapped[int | None] = mapped_column(sa.Integer)

    __table_args__ = (
        sa.Index("ix_sync_log_channel_ts", "channel", "ts"),
        sa.Index("ix_sync_log_trace_id", "trace_id"),
    )


class ChannelHealth(Base):
    """Состояние каналов для /health и алертов о молчании."""

    __tablename__ = "channel_health"

    channel: Mapped[SyncChannel] = mapped_column(enum_column(SyncChannel), primary_key=True)
    last_success_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    last_error_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    consecutive_errors: Mapped[int] = mapped_column(sa.Integer, default=0)
    #: Чтобы алерт «канал молчит больше часа» не повторялся каждую минуту.
    alerted_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)


class Reminder(Base):
    """Запланированное напоминание ученику. Шлём только частным — на Preply свои."""

    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(primary_key=True)
    lesson_id: Mapped[int] = mapped_column(sa.ForeignKey("lessons.id", ondelete="CASCADE"))
    hours_before: Mapped[int] = mapped_column(sa.Integer)
    scheduled_utc: Mapped[dt.datetime] = mapped_column(UtcDateTime)
    sent_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)

    lesson: Mapped[Lesson] = relationship()

    __table_args__ = (
        sa.UniqueConstraint("lesson_id", "hours_before", name="uq_reminder_per_lesson"),
        sa.Index(
            "ix_reminders_due",
            "scheduled_utc",
            postgresql_where=sa.text("sent_at IS NULL"),
            sqlite_where=sa.text("sent_at IS NULL"),
        ),
    )
