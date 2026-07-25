"""Фоновый процесс.

Планировщик живёт только здесь. Если бы APScheduler поднимался в каждом из трёх
процессов, каждая периодическая задача выполнялась бы трижды — сверка расписания
и рассылка напоминаний этого не прощают.

Сейчас крутятся две задачи: проверка живости базы и разбор очереди внешних
эффектов. Приём писем (этап 3) и напоминания (этап 4) подключаются сюда же.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import sqlalchemy as sa
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.ext.asyncio import AsyncSession

from tutorsync import __version__
from tutorsync.config import get_settings
from tutorsync.db.models import ChannelHealth
from tutorsync.db.session import dispose_engine, session_scope
from tutorsync.enums import SyncChannel
from tutorsync.logging import get_logger, trace
from tutorsync.services import notify
from tutorsync.tasks import outbox

log = get_logger(__name__)


async def heartbeat() -> None:
    """Отмечает, что процесс жив и база доступна.

    Пишет в channel_health, а не только в лог: /health в боте и алерт «канал
    молчит дольше часа» читают именно эту таблицу, и им нужен факт в базе,
    а не строка в stdout.
    """
    with trace(channel="worker"):
        try:
            async with session_scope() as session:
                await session.execute(sa.text("SELECT 1"))
                await _mark_success(session, SyncChannel.OUTBOX)
            log.debug("worker.heartbeat.ok")
        except Exception as exc:
            log.error("worker.heartbeat.failed", error=str(exc))


async def _mark_success(session: AsyncSession, channel: SyncChannel) -> None:
    now = dt.datetime.now(dt.UTC)
    row = await session.get(ChannelHealth, channel)
    if row is None:
        session.add(
            ChannelHealth(channel=channel, last_success_at=now, consecutive_errors=0)
        )
        return
    row.last_success_at = now
    row.consecutive_errors = 0
    row.alerted_at = None


async def drain_outbox() -> None:
    """Разбирает очередь внешних эффектов.

    Ошибки конкретных заданий обрабатываются внутри process_once; сюда долетает
    только то, что сломалось до них — например, недоступная база. Планировщик
    APScheduler по умолчанию проглатывает исключения задачи молча, поэтому
    ловим и логируем сами.
    """
    with trace(channel="outbox"):
        try:
            handled = await outbox.process_once()
            if handled:
                log.info("outbox.drained", handled=handled)
        # Планировщик не должен останавливаться из-за одного неудачного прохода.
        except Exception as exc:
            log.error("outbox.drain_failed", error=str(exc))


async def run_worker() -> None:
    settings = get_settings()
    scheduler = AsyncIOScheduler(timezone=dt.UTC)
    scheduler.add_job(
        heartbeat,
        "interval",
        minutes=1,
        id="heartbeat",
        # Если процесс подвис и пропустил несколько запусков, догонять их
        # не нужно — достаточно одного свежего.
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        drain_outbox,
        "interval",
        seconds=15,
        id="outbox",
        coalesce=True,
        # max_instances=1 здесь принципиально: два одновременных прохода
        # разобрали бы одни и те же задания на SQLite, где нет SKIP LOCKED.
        max_instances=1,
    )
    scheduler.start()
    log.info(
        "worker.started",
        version=__version__,
        reconcile_interval_min=settings.reconcile_interval_min,
    )

    stop = asyncio.Event()
    try:
        await stop.wait()
    finally:
        scheduler.shutdown(wait=False)
        await notify.close()
        await dispose_engine()
        log.info("worker.stopped")


def main() -> None:
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        pass
