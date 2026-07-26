"""Единая точка входа.

Образ один на все процессы, роль задаётся переменной ROLE — так деплой
в Coolify сводится к трём ресурсам с разным окружением и одной сборке.

    python -m tutorsync             # запустить процесс, роль из ROLE
    python -m tutorsync migrate     # применить миграции и выйти
    python -m tutorsync reset       # показать, что создал сервис
    python -m tutorsync reset --yes # убрать созданное сервисом
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys

from tutorsync.config import Role, get_settings
from tutorsync.logging import configure_logging, get_logger

log = get_logger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tutorsync",
        description="Синхронизация расписания. Без команды запускает процесс из ROLE.",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("migrate", help="применить миграции базы и выйти")

    reset = sub.add_parser(
        "reset",
        help="убрать всё, что создал сервис (для отладки)",
        description=(
            "Удаляет события, созданные сервисом, и очищает данные расписания. "
            "Чужие события в календарях, авторизация Google и рабочие часы "
            "не затрагиваются. Без --yes только показывает план."
        ),
    )
    reset.add_argument(
        "--yes",
        action="store_true",
        help="выполнить удаление; без флага команда только показывает план",
    )
    reset.add_argument(
        "--with-students",
        action="store_true",
        help="дополнительно стереть зарегистрированных учеников",
    )
    reset.add_argument(
        "--days",
        type=int,
        default=365,
        help="на сколько дней в обе стороны просматривать календари (по умолчанию 365)",
    )
    return parser


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


async def _reset(*, days: int, with_students: bool, confirmed: bool) -> int:
    from tutorsync.db.session import dispose_engine, session_scope
    from tutorsync.gcal.client import CalendarClient
    from tutorsync.gcal.oauth import NoCredentialsError, OAuthNotConfiguredError, load_credentials
    from tutorsync.services.cleanup import apply_plan, build_plan, render_plan

    settings = get_settings()
    try:
        async with session_scope() as session:
            try:
                client = CalendarClient(await load_credentials(session))
            except (NoCredentialsError, OAuthNotConfiguredError):
                # Без авторизации чистится только база. Это рабочий случай:
                # так выглядит сброс до подключения Google-аккаунта.
                client = None

            plan = await build_plan(
                session,
                client,
                settings,
                now=dt.datetime.now(dt.UTC),
                days=days,
                with_students=with_students,
            )
            print(render_plan(plan, with_students=with_students))

            if plan.is_empty:
                print("\nУдалять нечего — состояние уже чистое.")
                return 0

            if not confirmed:
                print(
                    "\nЭто предпросмотр, ничего не изменено."
                    "\nЧтобы выполнить: python -m tutorsync reset --yes"
                )
                return 0

            report = await apply_plan(session, client, plan, with_students=with_students)

        print(f"\nУдалено событий: {report.events_deleted}")
        print(f"Удалено строк: {sum(report.rows_deleted.values())}")
        if report.events_failed:
            # Частичный откат хуже полного отсутствия отката: о нём надо знать
            # сразу, а не обнаружить остаток в календаре через неделю.
            print(f"\nНе удалось удалить событий: {len(report.events_failed)}")
            for event_id, error in report.events_failed[:10]:
                print(f"  {event_id}: {error}")
            return 1
        return 0
    finally:
        await dispose_engine()


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
    args = _build_parser().parse_args()
    settings = get_settings()
    configure_logging(settings)

    if args.command == "migrate":
        # Миграциям нужен только адрес базы: гонять их через полную проверку
        # секретов значило бы требовать токен бота для applying schema.
        _run_migrations()
        return

    if args.command == "reset":
        sys.exit(asyncio.run(_reset(
            days=args.days,
            with_students=args.with_students,
            confirmed=args.yes,
        )))

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
