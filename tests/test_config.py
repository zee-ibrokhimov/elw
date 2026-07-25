"""Тесты разбора конфигурации.

Списочные переменные (`LESSON_DURATIONS=30,60,90`) — то место, где
pydantic-settings по умолчанию пытается разобрать значение как JSON и падает.
Тесты фиксируют, что этого не происходит, и что ошибка в .env всплывает на
старте процесса, а не через неделю в проде.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tutorsync.config import Role


def test_csv_lists_parsed(env):
    settings = env(
        LESSON_DURATIONS="90, 30,60",
        ADMIN_TELEGRAM_IDS="111, 222",
        REMINDER_HOURS_BEFORE="1,24",
        EXTRA_BUSY_CALENDAR_IDS="primary, work@group.calendar.google.com",
    )

    assert settings.durations == (30, 60, 90)
    assert settings.admin_ids == frozenset({111, 222})
    # Напоминания идут от дальнего к ближнему — в этом порядке они и рассылаются.
    assert settings.reminder_offsets_hours == (24, 1)
    assert settings.extra_busy_calendars == ("primary", "work@group.calendar.google.com")


def test_language_defaults_to_first(env):
    assert env(STUDENT_LANGUAGES="en,ru").default_language == "en"


def test_webhook_path_is_secret_and_absolute(env):
    settings = env(
        PUBLIC_BASE_URL="https://tutorsync.example.com/",
        GCAL_WEBHOOK_PATH_SECRET="s3cr3t",
    )

    assert settings.gcal_webhook_path == "/webhook/gcal/s3cr3t"
    # Лишний слэш в PUBLIC_BASE_URL не должен превращаться в двойной в URL:
    # Google откажется регистрировать канал на некорректный адрес.
    assert settings.gcal_webhook_url == "https://tutorsync.example.com/webhook/gcal/s3cr3t"


def test_unknown_timezone_rejected(env):
    with pytest.raises(ValidationError):
        env(OWNER_TZ="Europe/Atlantis")


def test_empty_durations_rejected(env):
    with pytest.raises(ValidationError):
        env(LESSON_DURATIONS="")


def test_bot_requires_token(env):
    settings = env(ROLE="bot", TELEGRAM_BOT_TOKEN="")

    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        settings.require_runtime_secrets()


@pytest.mark.parametrize("role", [Role.WEB, Role.WORKER, Role.BOT])
def test_unconfigured_features_do_not_block_startup(env, role: Role):
    """Незаполненное для будущих этапов не должно ронять процесс.

    Иначе сервис нельзя развернуть и проверить, пока не настроено вообще всё:
    ни цепочку деплоя, ни доступность базы.
    """
    settings = env(ROLE=role.value)

    settings.require_runtime_secrets()  # не должно бросать


@pytest.mark.parametrize(
    ("role", "expected_variable"),
    [
        (Role.WORKER, "IMAP_USER"),
        (Role.WEB, "GCAL_WEBHOOK_TOKEN"),
    ],
)
def test_pending_settings_reported_per_role(env, role: Role, expected_variable: str):
    """Каждой роли своё: боту не нужен доступ к почте, воркеру — секрет вебхука."""
    settings = env(ROLE=role.value)

    pending = settings.missing_optional()

    assert expected_variable in pending
    # Отчёт должен объяснять, что именно не заработает, а не только чего не хватает.
    assert pending[expected_variable]


def test_configured_google_not_reported(env):
    settings = env(
        ROLE="bot",
        GOOGLE_CLIENT_ID="id",
        GOOGLE_CLIENT_SECRET="secret",
        GCAL_CALENDAR_PREPLY="a@group.calendar.google.com",
        GCAL_CALENDAR_PRIVATE="b@group.calendar.google.com",
        GCAL_CALENDAR_BUFFERS="c@group.calendar.google.com",
    )

    assert settings.missing_optional() == {}
