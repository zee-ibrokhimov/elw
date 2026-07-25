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


@pytest.mark.parametrize(
    ("role", "expected_missing"),
    [
        (Role.WORKER, "IMAP_USER"),
        (Role.WEB, "GCAL_WEBHOOK_TOKEN"),
    ],
)
def test_missing_secrets_reported_per_role(env, role: Role, expected_missing: str):
    """Каждая роль требует своего набора секретов.

    Боту не нужен доступ к почте, воркеру — секрет вебхука. Общая проверка
    заставляла бы заполнять всё ради запуска одного процесса.
    """
    settings = env(ROLE=role.value)

    with pytest.raises(RuntimeError, match=expected_missing):
        settings.require_runtime_secrets()


def test_bot_role_needs_no_google_or_imap(env):
    settings = env(ROLE="bot")

    settings.require_runtime_secrets()  # не должно бросать
