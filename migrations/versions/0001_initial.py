"""Начальная схема.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Перечисления хранятся как VARCHAR: нативный ENUM в Postgres требует
#: ALTER TYPE при добавлении значения и не существует в SQLite.
ENUM = sa.String(32)

TS = sa.DateTime(timezone=True)

JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    # --- люди и расписание ---------------------------------------------------

    op.create_table(
        "students",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tg_user_id", sa.BigInteger(), nullable=True),
        sa.Column("tg_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("language", sa.String(8), nullable=False),
        sa.Column("tz", sa.String(64), nullable=False),
        # telegram | preply
        sa.Column("source", ENUM, nullable=False),
        sa.Column("consent_at", TS, nullable=True),
        sa.Column("anonymized_at", TS, nullable=True),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_students"),
        sa.UniqueConstraint("tg_user_id", name="uq_students_tg_user_id"),
    )

    op.create_table(
        "lessons",
        sa.Column("id", sa.Integer(), nullable=False),
        # preply | private | block
        sa.Column("source", ENUM, nullable=False),
        sa.Column("external_id", sa.String(128), nullable=True),
        sa.Column("student_id", sa.Integer(), nullable=True),
        sa.Column("student_name_raw", sa.String(128), nullable=True),
        sa.Column("start_utc", TS, nullable=False),
        sa.Column("end_utc", TS, nullable=False),
        sa.Column("duration_min", sa.Integer(), nullable=False),
        # scheduled | cancelled | completed
        sa.Column("status", ENUM, nullable=False),
        sa.Column("booked_tz", sa.String(64), nullable=True),
        sa.Column("replaces_lesson_id", sa.Integer(), nullable=True),
        sa.Column("cancelled_at", TS, nullable=True),
        sa.Column("cancel_reason", sa.String(256), nullable=True),
        sa.Column("note", sa.String(256), nullable=True),
        sa.Column("conflict_accepted", sa.Boolean(), nullable=False),
        sa.Column("sync_version", sa.Integer(), nullable=False),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_lessons"),
        sa.ForeignKeyConstraint(
            ["student_id"],
            ["students.id"],
            name="fk_lessons_student_id_students",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["replaces_lesson_id"],
            ["lessons.id"],
            name="fk_lessons_replaces_lesson_id_lessons",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint("end_utc > start_utc", name="ck_lessons_end_after_start"),
        sa.CheckConstraint("duration_min > 0", name="ck_lessons_positive_duration"),
    )
    op.create_index("ix_lessons_start_utc", "lessons", ["start_utc"])
    op.create_index("ix_lessons_status_start", "lessons", ["status", "start_utc"])
    # Идемпотентность: повторная доставка того же письма Preply или того же
    # вебхука не должна создавать вторую запись.
    op.create_index(
        "uq_lessons_source_external_id",
        "lessons",
        ["source", "external_id"],
        unique=True,
        postgresql_where=sa.text("external_id IS NOT NULL"),
        sqlite_where=sa.text("external_id IS NOT NULL"),
    )
    op.create_index(
        "uq_lessons_student_start",
        "lessons",
        ["student_id", "start_utc"],
        unique=True,
        postgresql_where=sa.text("status = 'scheduled' AND student_id IS NOT NULL"),
        sqlite_where=sa.text("status = 'scheduled' AND student_id IS NOT NULL"),
    )

    op.create_table(
        "busy_intervals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("lesson_id", sa.Integer(), nullable=False),
        # lesson | buffer_before | buffer_after
        sa.Column("role", ENUM, nullable=False),
        sa.Column("start_utc", TS, nullable=False),
        sa.Column("end_utc", TS, nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("conflict_accepted", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_busy_intervals"),
        sa.ForeignKeyConstraint(
            ["lesson_id"],
            ["lessons.id"],
            name="fk_busy_intervals_lesson_id_lessons",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("end_utc > start_utc", name="ck_busy_intervals_end_after_start"),
    )
    op.create_index("ix_busy_intervals_range", "busy_intervals", ["start_utc", "end_utc"])
    op.create_index("ix_busy_intervals_lesson_id", "busy_intervals", ["lesson_id"])

    # Защита от двойного бронирования на уровне базы.
    #
    # Предикат разбирается по частям:
    #   role = 'lesson'        — буферы соседних уроков при плотной сетке
    #                            законно накладываются, их проверять нельзя;
    #   active                 — отменённые интервалы места не занимают;
    #   NOT conflict_accepted  — оплаченный урок с Preply принимается в базу
    #                            даже поверх существующей брони: отменить его
    #                            нельзя, поэтому вместо отказа заводится
    #                            запись в conflicts и уходит алерт админу.
    #
    # На SQLite аналога нет — там ту же инвариантность держит репозиторий
    # через BEGIN IMMEDIATE и проверку пересечений в той же транзакции.
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            ALTER TABLE busy_intervals
            ADD CONSTRAINT ex_busy_intervals_no_lesson_overlap
            EXCLUDE USING gist (tstzrange(start_utc, end_utc, '[)') WITH &&)
            WHERE (role = 'lesson' AND active AND NOT conflict_accepted)
            """
        )

    # --- google calendar -----------------------------------------------------

    op.create_table(
        "calendar_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("lesson_id", sa.Integer(), nullable=False),
        # preply | private | buffers
        sa.Column("calendar_key", ENUM, nullable=False),
        sa.Column("gcal_calendar_id", sa.String(256), nullable=False),
        sa.Column("gcal_event_id", sa.String(256), nullable=False),
        # lesson | buffer_before | buffer_after | busy_mirror
        sa.Column("role", ENUM, nullable=False),
        sa.Column("sync_version", sa.Integer(), nullable=False),
        sa.Column("etag", sa.String(128), nullable=True),
        sa.Column("last_synced_at", TS, nullable=True),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_calendar_events"),
        sa.ForeignKeyConstraint(
            ["lesson_id"],
            ["lessons.id"],
            name="fk_calendar_events_lesson_id_lessons",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("gcal_calendar_id", "gcal_event_id", name="uq_calendar_event"),
    )
    op.create_index("ix_calendar_events_lesson_id", "calendar_events", ["lesson_id"])

    op.create_table(
        "oauth_credentials",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_email", sa.String(256), nullable=False),
        # Fernet-шифртекст; ключ живёт в SECRET_ENC_KEY и в базу не попадает.
        sa.Column("refresh_token_enc", sa.Text(), nullable=False),
        sa.Column("access_token_enc", sa.Text(), nullable=True),
        sa.Column("token_expiry", TS, nullable=True),
        sa.Column("scopes", sa.Text(), nullable=False),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_oauth_credentials"),
        sa.UniqueConstraint("account_email", name="uq_oauth_credentials_account_email"),
    )

    op.create_table(
        "gcal_channels",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("calendar_id", sa.String(256), nullable=False),
        sa.Column("channel_id", sa.String(128), nullable=True),
        sa.Column("resource_id", sa.String(256), nullable=True),
        sa.Column("token", sa.String(128), nullable=True),
        sa.Column("expiration", TS, nullable=True),
        sa.Column("sync_token", sa.Text(), nullable=True),
        sa.Column("last_sync_at", TS, nullable=True),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_gcal_channels"),
        sa.UniqueConstraint("calendar_id", name="uq_gcal_channels_calendar_id"),
    )

    # --- ручные действия и конфликты -----------------------------------------

    op.create_table(
        "manual_tasks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("lesson_id", sa.Integer(), nullable=False),
        # close_slot | reopen_slot
        sa.Column("action", ENUM, nullable=False),
        # pending | done | obsolete
        sa.Column("status", ENUM, nullable=False),
        sa.Column("tg_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("tg_message_id", sa.BigInteger(), nullable=True),
        sa.Column("reminders_sent", sa.Integer(), nullable=False),
        sa.Column("last_reminded_at", TS, nullable=True),
        sa.Column("done_at", TS, nullable=True),
        sa.Column("done_by_tg_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_manual_tasks"),
        sa.ForeignKeyConstraint(
            ["lesson_id"],
            ["lessons.id"],
            name="fk_manual_tasks_lesson_id_lessons",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_manual_tasks_status", "manual_tasks", ["status"])
    op.create_index(
        "uq_manual_tasks_lesson_action_pending",
        "manual_tasks",
        ["lesson_id", "action"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
        sqlite_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "conflicts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("lesson_a_id", sa.Integer(), nullable=False),
        sa.Column("lesson_b_id", sa.Integer(), nullable=False),
        # open | resolved | ignored
        sa.Column("status", ENUM, nullable=False),
        # keep_both | cancelled_private | handled_manually
        sa.Column("resolution", ENUM, nullable=True),
        sa.Column("resolved_at", TS, nullable=True),
        sa.Column("resolved_by_tg_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_conflicts"),
        sa.ForeignKeyConstraint(
            ["lesson_a_id"],
            ["lessons.id"],
            name="fk_conflicts_lesson_a_id_lessons",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["lesson_b_id"],
            ["lessons.id"],
            name="fk_conflicts_lesson_b_id_lessons",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("lesson_a_id", "lesson_b_id", name="uq_conflict_pair"),
        sa.CheckConstraint("lesson_a_id <> lesson_b_id", name="ck_conflicts_distinct_lessons"),
    )

    # --- рабочее расписание --------------------------------------------------

    op.create_table(
        "working_hours",
        sa.Column("id", sa.Integer(), nullable=False),
        # 0 — понедельник, 6 — воскресенье, как в date.weekday()
        sa.Column("weekday", sa.SmallInteger(), nullable=False),
        sa.Column("start_local", sa.Time(), nullable=False),
        sa.Column("end_local", sa.Time(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_working_hours"),
        sa.CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_working_hours_weekday_range"),
    )
    op.create_index("ix_working_hours_weekday", "working_hours", ["weekday"])

    op.create_table(
        "schedule_exceptions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("date_from", sa.Date(), nullable=False),
        sa.Column("date_to", sa.Date(), nullable=False),
        # closed | extra
        sa.Column("kind", ENUM, nullable=False),
        sa.Column("start_local", sa.Time(), nullable=True),
        sa.Column("end_local", sa.Time(), nullable=True),
        sa.Column("note", sa.String(256), nullable=True),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_schedule_exceptions"),
        sa.CheckConstraint("date_to >= date_from", name="ck_schedule_exceptions_date_range"),
    )
    op.create_index(
        "ix_schedule_exceptions_dates", "schedule_exceptions", ["date_from", "date_to"]
    )

    # --- доставка эффектов, приём писем, наблюдаемость -----------------------

    op.create_table(
        "outbox",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", ENUM, nullable=False),
        sa.Column("payload", JSON_TYPE, nullable=False),
        # pending | done | failed
        sa.Column("status", ENUM, nullable=False),
        sa.Column("run_after", TS, nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("trace_id", sa.String(32), nullable=True),
        sa.Column("dedup_key", sa.String(200), nullable=True),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("done_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_outbox"),
    )
    op.create_index("ix_outbox_ready", "outbox", ["status", "run_after"])
    op.create_index(
        "uq_outbox_dedup_key",
        "outbox",
        ["dedup_key"],
        unique=True,
        postgresql_where=sa.text("dedup_key IS NOT NULL AND status = 'pending'"),
        sqlite_where=sa.text("dedup_key IS NOT NULL AND status = 'pending'"),
    )

    op.create_table(
        "processed_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.String(512), nullable=False),
        sa.Column("imap_uid", sa.BigInteger(), nullable=True),
        sa.Column("parser", sa.String(64), nullable=True),
        sa.Column("processed_at", TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_processed_messages"),
        sa.UniqueConstraint("message_id", name="uq_processed_messages_message_id"),
    )

    op.create_table(
        "dead_letters",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("message_id", sa.String(512), nullable=True),
        sa.Column("sender", sa.String(256), nullable=True),
        sa.Column("subject", sa.String(512), nullable=True),
        sa.Column("raw", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("trace_id", sa.String(32), nullable=True),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("resolved_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_dead_letters"),
    )

    op.create_table(
        "sync_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ts", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("trace_id", sa.String(32), nullable=True),
        # gcal | imap | telegram | reconcile | outbox
        sa.Column("channel", ENUM, nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("entity_type", sa.String(64), nullable=True),
        sa.Column("entity_id", sa.String(128), nullable=True),
        # ok | error | skipped
        sa.Column("result", ENUM, nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_sync_log"),
    )
    op.create_index("ix_sync_log_channel_ts", "sync_log", ["channel", "ts"])
    op.create_index("ix_sync_log_trace_id", "sync_log", ["trace_id"])

    op.create_table(
        "channel_health",
        sa.Column("channel", ENUM, nullable=False),
        sa.Column("last_success_at", TS, nullable=True),
        sa.Column("last_error_at", TS, nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("consecutive_errors", sa.Integer(), nullable=False),
        sa.Column("alerted_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("channel", name="pk_channel_health"),
    )

    op.create_table(
        "reminders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("lesson_id", sa.Integer(), nullable=False),
        sa.Column("hours_before", sa.Integer(), nullable=False),
        sa.Column("scheduled_utc", TS, nullable=False),
        sa.Column("sent_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_reminders"),
        sa.ForeignKeyConstraint(
            ["lesson_id"],
            ["lessons.id"],
            name="fk_reminders_lesson_id_lessons",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("lesson_id", "hours_before", name="uq_reminder_per_lesson"),
    )
    op.create_index(
        "ix_reminders_due",
        "reminders",
        ["scheduled_utc"],
        postgresql_where=sa.text("sent_at IS NULL"),
        sqlite_where=sa.text("sent_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("reminders")
    op.drop_table("channel_health")
    op.drop_table("sync_log")
    op.drop_table("dead_letters")
    op.drop_table("processed_messages")
    op.drop_table("outbox")
    op.drop_table("schedule_exceptions")
    op.drop_table("working_hours")
    op.drop_table("conflicts")
    op.drop_table("manual_tasks")
    op.drop_table("gcal_channels")
    op.drop_table("oauth_credentials")
    op.drop_table("calendar_events")
    op.drop_table("busy_intervals")
    op.drop_table("lessons")
    op.drop_table("students")
