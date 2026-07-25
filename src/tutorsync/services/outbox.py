"""Постановка внешних эффектов в очередь и выборка их на исполнение.

Правило, ради которого всё это существует: наружу изнутри транзакции не ходим.
Запись расписания и вызов Google атомарными быть не могут, поэтому в транзакции
появляется только строка «сделать то-то», а делает её отдельный воркер — уже
после коммита и с ретраями.

Обратное — вызвать Google прямо в транзакции — выглядит проще ровно до первого
сетевого таймаута, после которого в базе есть урок, которого нет в календаре.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tutorsync.db.models import OutboxJob
from tutorsync.enums import OutboxKind, OutboxStatus
from tutorsync.logging import current_trace_id, get_logger

log = get_logger(__name__)

#: После скольких неудач задание признаётся безнадёжным. Восемь попыток с
#: экспоненциальной паузой растягиваются примерно на два часа — этого хватает,
#: чтобы пережить любой разумный сбой Google, и мало, чтобы задание зависло
#: в очереди на сутки незамеченным.
MAX_ATTEMPTS = 8

#: Пауза перед первой повторной попыткой; дальше удваивается до потолка.
BASE_BACKOFF_SEC = 30
MAX_BACKOFF_SEC = 3600


def backoff_delay(attempts: int) -> dt.timedelta:
    return dt.timedelta(seconds=min(BASE_BACKOFF_SEC * 2 ** max(attempts - 1, 0), MAX_BACKOFF_SEC))


async def enqueue(
    session: AsyncSession,
    kind: OutboxKind,
    payload: dict[str, Any],
    *,
    dedup_key: str | None = None,
    run_after: dt.datetime | None = None,
) -> OutboxJob | None:
    """Кладёт задание в ту же транзакцию, что и изменение расписания.

    Возвращает None, если задание с таким dedup_key уже ждёт исполнения. Это не
    ошибка, а норма: одно и то же изменение может прилететь дважды — повторной
    доставкой письма, повторным вебхуком, двойным нажатием кнопки.

    Вставка идёт во вложенной транзакции: гонка двух процессов ловится не
    предварительным SELECT (он ничего не гарантирует), а уникальным индексом,
    и откатывать при этом надо только саму вставку, не всю транзакцию с уроком.
    """
    job = OutboxJob(
        kind=kind,
        payload=payload,
        status=OutboxStatus.PENDING,
        run_after=run_after or dt.datetime.now(dt.UTC),
        attempts=0,
        dedup_key=dedup_key,
        trace_id=current_trace_id(),
    )
    try:
        async with session.begin_nested():
            session.add(job)
            await session.flush()
    except IntegrityError:
        log.debug("outbox.duplicate_skipped", kind=kind.value, dedup_key=dedup_key)
        return None

    log.debug("outbox.enqueued", kind=kind.value, job_id=job.id, dedup_key=dedup_key)
    return job


async def claim_jobs(session: AsyncSession, limit: int, *, is_postgres: bool) -> list[OutboxJob]:
    """Забирает пачку готовых заданий, помечая их своими на время обработки.

    FOR UPDATE SKIP LOCKED в Postgres позволяет запустить несколько воркеров, не
    боясь, что они возьмут одно и то же задание. Сейчас воркер один, но выбирать
    между «одним процессом» и «переписать очередь» при росте нагрузки не хочется.
    """
    stmt = (
        sa.select(OutboxJob)
        .where(
            OutboxJob.status == OutboxStatus.PENDING,
            OutboxJob.run_after <= dt.datetime.now(dt.UTC),
        )
        .order_by(OutboxJob.run_after, OutboxJob.id)
        .limit(limit)
    )
    if is_postgres:
        stmt = stmt.with_for_update(skip_locked=True)
    return list((await session.execute(stmt)).scalars().all())


async def mark_done(session: AsyncSession, job: OutboxJob) -> None:
    job.status = OutboxStatus.DONE
    job.done_at = dt.datetime.now(dt.UTC)
    job.last_error = None
    # dedup_key снимается вместе с завершением: частичный уникальный индекс
    # висит только на pending, и следующее такое же изменение должно проходить.
    job.dedup_key = None


async def mark_retry(session: AsyncSession, job: OutboxJob, error: str) -> None:
    job.attempts += 1
    job.last_error = error[:2000]
    job.run_after = dt.datetime.now(dt.UTC) + backoff_delay(job.attempts)


async def mark_failed(session: AsyncSession, job: OutboxJob, error: str) -> None:
    job.status = OutboxStatus.FAILED
    job.attempts += 1
    job.last_error = error[:2000]
    job.done_at = dt.datetime.now(dt.UTC)
    job.dedup_key = None
