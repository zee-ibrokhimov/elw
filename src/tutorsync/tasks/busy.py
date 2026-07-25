"""Периодический импорт чужой занятости.

Опрос, а не push-уведомления через ``events.watch``. Каналы дают выигрыш
в секундах, но требуют публичного вебхука, продления раз в неделю и разбора
случая «канал протух, пока сервис лежал». Здесь этот выигрыш не окупается:
Preply сам узнаёт об изменениях в календаре с задержкой в минуты, так что
опрос раз в несколько минут не является узким местом. Когда узким местом
станет — переключение будет локальным, база и расчёт слотов не изменятся.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.ext.asyncio import AsyncSession

from tutorsync.config import get_settings
from tutorsync.db.models import ChannelHealth
from tutorsync.db.session import session_scope
from tutorsync.enums import SyncChannel
from tutorsync.gcal.client import CalendarClient
from tutorsync.gcal.oauth import NoCredentialsError, load_credentials
from tutorsync.logging import get_logger, trace
from tutorsync.services import notify
from tutorsync.services.busy_import import import_busy

log = get_logger(__name__)


async def refresh_busy() -> dict[str, int] | None:
    """Один проход импорта. Возвращает статистику или None, если он невозможен."""
    settings = get_settings()
    if not settings.extra_busy_calendars:
        return None

    with trace(channel="gcal"):
        try:
            async with session_scope() as session:
                creds = await load_credentials(session)
            client = CalendarClient(creds)

            async with session_scope() as session:
                stats = await import_busy(session, client)
                await _mark_ok(session)
            return stats
        except NoCredentialsError:
            # Авторизация ещё не пройдена — это состояние настройки, а не сбой.
            log.debug("busy.no_credentials")
            return None
        # Падать планировщику нельзя: следующий проход должен состояться,
        # даже если этот упёрся в недоступный Google.
        except Exception as exc:
            log.error("busy.failed", error=str(exc))
            await _mark_error(str(exc))
            return None


async def _mark_ok(session: AsyncSession) -> None:
    now = dt.datetime.now(dt.UTC)
    row = await session.get(ChannelHealth, SyncChannel.GCAL)
    if row is None:
        session.add(
            ChannelHealth(channel=SyncChannel.GCAL, last_success_at=now, consecutive_errors=0)
        )
        return
    row.last_success_at = now
    row.consecutive_errors = 0
    row.alerted_at = None


async def _mark_error(error: str) -> None:
    """Отмечает неудачу и, дойдя до порога, будит админа один раз.

    Один раз, а не на каждой попытке: при недоступном Google проходы идут
    каждые несколько минут, и без отсечки алерт превратился бы в поток,
    который перестают читать.
    """
    settings = get_settings()
    async with session_scope() as session:
        row = await session.get(ChannelHealth, SyncChannel.GCAL)
        if row is None:
            row = ChannelHealth(channel=SyncChannel.GCAL, consecutive_errors=0)
            session.add(row)
        row.last_error_at = dt.datetime.now(dt.UTC)
        row.last_error = error[:2000]
        row.consecutive_errors += 1
        should_alert = (
            row.consecutive_errors >= settings.consecutive_errors_alert
            and row.alerted_at is None
        )
        if should_alert:
            row.alerted_at = dt.datetime.now(dt.UTC)
            errors = row.consecutive_errors

    if should_alert:
        await notify.send_admin(
            "⚠️ Не удаётся прочитать календари Google.\n"
            f"Неудачных попыток подряд: {errors}\n"
            f"Ошибка: <code>{error[:400]}</code>\n\n"
            "Пока это так, бот может предлагать ученикам уже занятое время."
        )
