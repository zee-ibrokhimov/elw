"""Конфигурация сервиса.

Все настройки приходят из окружения — в репозитории секретов нет.
Списочные значения объявлены строками и разбираются свойствами: pydantic-settings
пытается парсить поля-контейнеры как JSON, из-за чего `LESSON_DURATIONS=30,60,90`
падал бы с ошибкой разбора.
"""

from __future__ import annotations

import enum
from functools import cached_property, lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Role(str, enum.Enum):
    """Роль процесса. Образ один, поведение задаётся переменной ROLE."""

    BOT = "bot"
    WEB = "web"
    WORKER = "worker"


class InboundMode(str, enum.Enum):
    """Что делать с правками, сделанными в Google Calendar руками."""

    DETECT = "detect"
    IMPORT = "import"


class LogFormat(str, enum.Enum):
    JSON = "json"
    CONSOLE = "console"


def _split_csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- процесс -------------------------------------------------------------
    role: Role = Role.WORKER
    owner_tz: str = "Europe/Rome"
    log_level: str = "info"
    log_format: LogFormat = LogFormat.JSON

    # --- база ----------------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:///./tutorsync.sqlite3"

    # --- шифрование ----------------------------------------------------------
    secret_enc_key: str = ""

    # --- telegram ------------------------------------------------------------
    telegram_bot_token: str = ""
    admin_telegram_ids: str = ""
    student_languages: str = "ru,en"

    # --- публичный адрес -----------------------------------------------------
    public_base_url: str = "http://localhost:8080"
    web_port: int = 8080

    # --- google --------------------------------------------------------------
    google_client_id: str = ""
    google_client_secret: str = ""
    google_oauth_redirect_uri: str = "http://localhost:8080/oauth/callback"

    gcal_calendar_preply: str = ""
    gcal_calendar_private: str = ""
    gcal_calendar_buffers: str = ""
    extra_busy_calendar_ids: str = ""

    #: Считать ли занятостью события на весь день из читаемых календарей.
    #: По умолчанию нет: дни рождения и праздники заняли бы сутки целиком,
    #: а отпуск задаётся через schedule_exceptions с явными границами.
    busy_include_all_day: bool = False
    #: Насколько назад тянуть чужую занятость. Сутки — чтобы урок, начавшийся
    #: вчера вечером и идущий за полночь, не пропал из расчёта.
    busy_import_past_days: int = Field(default=1, ge=0)
    busy_import_interval_min: int = Field(default=5, gt=0)

    gcal_inbound_mode: InboundMode = InboundMode.DETECT
    gcal_webhook_token: str = ""
    gcal_webhook_path_secret: str = ""

    # --- imap ----------------------------------------------------------------
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_folder: str = "Preply"
    imap_allowed_senders: str = "@preply.com"
    imap_poll_interval_sec: int = 300
    imap_idle_timeout_sec: int = 840

    # --- preply --------------------------------------------------------------
    preply_gcal_linked: bool = False
    manual_task_remind_interval_min: int = 30
    manual_task_max_reminders: int = 6
    preply_busy_mirror: bool = False

    # --- правила бронирования ------------------------------------------------
    lesson_durations: str = "30,60,90"
    slot_grid_minutes: int = Field(default=30, gt=0)
    min_notice_hours: int = Field(default=24, ge=0)
    booking_horizon_days: int = Field(default=30, gt=0)
    cancel_deadline_hours: int = Field(default=12, ge=0)
    max_lessons_per_day: int = Field(default=0, ge=0)

    # --- буферы --------------------------------------------------------------
    buffer_before_min: int = Field(default=10, ge=0)
    buffer_after_min: int = Field(default=10, ge=0)

    # --- напоминания ---------------------------------------------------------
    reminder_hours_before: str = "24,1"

    # --- фоновые задачи ------------------------------------------------------
    reconcile_interval_min: int = Field(default=15, gt=0)
    gcal_watch_renew_before_hours: int = Field(default=24, gt=0)
    stale_channel_alert_min: int = Field(default=60, gt=0)
    consecutive_errors_alert: int = Field(default=3, gt=0)

    # --- антифлуд ------------------------------------------------------------
    rate_limit_messages: int = Field(default=20, gt=0)
    rate_limit_window_sec: int = Field(default=60, gt=0)

    # --- хранение данных -----------------------------------------------------
    data_retention_days: int = Field(default=730, ge=0)

    # === разбор списков ======================================================

    @cached_property
    def admin_ids(self) -> frozenset[int]:
        return frozenset(int(x) for x in _split_csv(self.admin_telegram_ids))

    @cached_property
    def languages(self) -> tuple[str, ...]:
        return tuple(_split_csv(self.student_languages)) or ("ru",)

    @cached_property
    def default_language(self) -> str:
        return self.languages[0]

    @cached_property
    def durations(self) -> tuple[int, ...]:
        return tuple(sorted(int(x) for x in _split_csv(self.lesson_durations)))

    @cached_property
    def reminder_offsets_hours(self) -> tuple[int, ...]:
        return tuple(sorted((int(x) for x in _split_csv(self.reminder_hours_before)), reverse=True))

    @cached_property
    def extra_busy_calendars(self) -> tuple[str, ...]:
        return tuple(_split_csv(self.extra_busy_calendar_ids))

    @cached_property
    def allowed_senders(self) -> tuple[str, ...]:
        return tuple(s.lower() for s in _split_csv(self.imap_allowed_senders))

    # === производные значения ================================================

    @cached_property
    def owner_zone(self) -> ZoneInfo:
        return ZoneInfo(self.owner_tz)

    @cached_property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")

    @cached_property
    def gcal_webhook_path(self) -> str:
        """Непредсказуемый путь вебхука — первый рубеж защиты, второй — channel token."""
        return f"/webhook/gcal/{self.gcal_webhook_path_secret}"

    @cached_property
    def gcal_webhook_url(self) -> str:
        return f"{self.public_base_url.rstrip('/')}{self.gcal_webhook_path}"

    @cached_property
    def managed_calendars(self) -> dict[str, str]:
        """Календари, в которые сервис пишет: ключ -> calendarId."""
        return {
            "preply": self.gcal_calendar_preply,
            "private": self.gcal_calendar_private,
            "buffers": self.gcal_calendar_buffers,
        }

    # === валидация ===========================================================

    @field_validator("owner_tz")
    @classmethod
    def _check_tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:  # pragma: no cover - зависит от окружения
            raise ValueError(
                f"Неизвестный часовой пояс {value!r}. "
                "Если это Windows или slim-образ — проверь, что установлен пакет tzdata."
            ) from exc
        return value

    @field_validator("log_level")
    @classmethod
    def _check_log_level(cls, value: str) -> str:
        allowed = {"debug", "info", "warning", "error", "critical"}
        lowered = value.lower()
        if lowered not in allowed:
            raise ValueError(f"LOG_LEVEL должен быть одним из {sorted(allowed)}, получено {value!r}")
        return lowered

    @model_validator(mode="after")
    def _check_lists(self) -> Settings:
        if not self.durations:
            raise ValueError("LESSON_DURATIONS не может быть пустым")
        if any(d <= 0 for d in self.durations):
            raise ValueError("LESSON_DURATIONS должны быть положительными")
        if any(h < 0 for h in self.reminder_offsets_hours):
            raise ValueError("REMINDER_HOURS_BEFORE не может содержать отрицательные значения")
        return self

    def require_runtime_secrets(self) -> None:
        """Проверка секретов, без которых процесс не сможет работать.

        Требуется ровно то, что задействовано уже написанным кодом. Настройки
        функций, которые ещё не подключены, сюда не входят: иначе сервис нельзя
        было бы развернуть и проверить, не настроив заранее всё до последней
        переменной. Недостающее для будущих этапов возвращает missing_optional().

        Вызывается из точки входа, а не из валидатора: alembic и тесты
        обходятся без секретов, и падать на импорте им незачем.
        """
        required: dict[str, str] = {
            "DATABASE_URL": self.database_url,
            "SECRET_ENC_KEY": self.secret_enc_key,
        }
        if self.role is Role.BOT:
            required |= {
                "TELEGRAM_BOT_TOKEN": self.telegram_bot_token,
                "ADMIN_TELEGRAM_IDS": self.admin_telegram_ids,
            }

        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError(
                f"Не заданы обязательные переменные окружения для ROLE={self.role.value}: "
                + ", ".join(missing)
            )

    def missing_optional(self) -> dict[str, str]:
        """Незаполненные настройки функций, которые подключаются позже.

        Возвращает имя переменной -> что из-за неё не заработает. Пишется в лог
        предупреждением на старте, чтобы забытая переменная всплыла сразу,
        а не в момент, когда ученик не получил напоминание.
        """
        pending: dict[str, str] = {}
        google = {
            "GOOGLE_CLIENT_ID": self.google_client_id,
            "GOOGLE_CLIENT_SECRET": self.google_client_secret,
            "GCAL_CALENDAR_PREPLY": self.gcal_calendar_preply,
            "GCAL_CALENDAR_PRIVATE": self.gcal_calendar_private,
            "GCAL_CALENDAR_BUFFERS": self.gcal_calendar_buffers,
        }
        for name, value in google.items():
            if not value:
                pending[name] = "синхронизация с Google Календарём"

        if self.role is Role.WEB:
            for name, value in {
                "GCAL_WEBHOOK_TOKEN": self.gcal_webhook_token,
                "GCAL_WEBHOOK_PATH_SECRET": self.gcal_webhook_path_secret,
            }.items():
                if not value:
                    pending[name] = "приём push-уведомлений Google"

        if self.role is Role.WORKER:
            for name, value in {
                "IMAP_USER": self.imap_user,
                "IMAP_PASSWORD": self.imap_password,
            }.items():
                if not value:
                    pending[name] = "приём писем Preply"

        return pending


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
