"""Единая точка входа.

Образ один на все процессы, роль задаётся переменной ROLE — так деплой
в Coolify сводится к трём ресурсам с разным окружением и одной сборке.

    python -m tutorsync            # роль берётся из ROLE
    python -m tutorsync migrate    # применить миграции и выйти
"""

from __future__ import annotations

import sys

from tutorsync.config import Role, get_settings
from tutorsync.logging import configure_logging, get_logger

log = get_logger(__name__)


def _run_migrations() -> None:
    from alembic import command
    from alembic.config import Config

    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("Не задан DATABASE_URL — применять миграции не к чему")

    cfg = Config("alembic.ini")
    log.info("migrate.start", database=_safe_dsn(settings.database_url))
    command.upgrade(cfg, "head")
    log.info("migrate.done")


def _safe_dsn(dsn: str) -> str:
    """Строка подключения без пароля — её можно писать в лог."""
    if "@" not in dsn:
        return dsn
    scheme_and_creds, _, host_part = dsn.rpartition("@")
    scheme, _, _creds = scheme_and_creds.partition("://")
    return f"{scheme}://***@{host_part}"


def _run_web() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "tutorsync.web.app:app",
        host="0.0.0.0",  # noqa: S104 — контейнер, наружу его выводит только Tunnel
        port=settings.web_port,
        # Логи уже структурные, свой формат uvicorn только ломает единый вывод.
        log_config=None,
        access_log=False,
    )


def main() -> None:
    settings = get_settings()
    configure_logging(settings)

    command_name = sys.argv[1] if len(sys.argv) > 1 else None

    if command_name == "migrate":
        # Миграциям нужен только адрес базы: гонять их через полную проверку
        # секретов значило бы требовать токен бота для applying schema.
        _run_migrations()
        return

    if command_name is not None:
        raise SystemExit(f"Неизвестная команда {command_name!r}. Доступна: migrate")

    settings.require_runtime_secrets()

    # Не заполненное для будущих этапов не мешает старту, но должно быть видно
    # в логе: молча запущенный процесс без половины настроек выглядит здоровым.
    for name, feature in settings.missing_optional().items():
        log.warning("config.not_configured", variable=name, disables=feature)

    log.info("process.starting", role=settings.role.value)

    match settings.role:
        case Role.WEB:
            _run_web()
        case Role.BOT:
            from tutorsync.bot.main import main as bot_main

            bot_main()
        case Role.WORKER:
            from tutorsync.tasks.runner import main as worker_main

            worker_main()


if __name__ == "__main__":
    main()
