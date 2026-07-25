"""Структурные логи со сквозным trace_id.

trace_id живёт в contextvar, поэтому автоматически попадает во все записи
внутри одной цепочки обработки — письма, вебхука, нажатия кнопки — включая
логи из вложенных корутин. Он же пишется в таблицу sync_log, так что по одному
идентификатору можно собрать всю историю операции через все воркеры.
"""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any

import structlog

from tutorsync.config import LogFormat, Settings

_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def current_trace_id() -> str | None:
    return _trace_id.get()


def set_trace_id(trace_id: str) -> Token[str | None]:
    return _trace_id.set(trace_id)


@contextmanager
def trace(trace_id: str | None = None, **bindings: Any) -> Iterator[str]:
    """Открывает новую цепочку обработки.

    >>> with trace(channel="imap", message_id=uid) as tid:
    ...     ...
    """
    tid = trace_id or new_trace_id()
    token = _trace_id.set(tid)
    structlog.contextvars.bind_contextvars(trace_id=tid, **bindings)
    try:
        yield tid
    finally:
        structlog.contextvars.unbind_contextvars("trace_id", *bindings.keys())
        _trace_id.reset(token)


def _add_trace_id(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    tid = _trace_id.get()
    if tid and "trace_id" not in event_dict:
        event_dict["trace_id"] = tid
    return event_dict


def configure_logging(settings: Settings) -> None:
    level = getattr(logging, settings.log_level.upper())

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    # Библиотеки любят логировать на INFO каждую HTTP-транзакцию — это шум,
    # из-за которого не видно собственных событий сервиса.
    for noisy in ("googleapiclient.discovery_cache", "httpx", "aiosqlite", "apscheduler.executors"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        _add_trace_id,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if settings.log_format is LogFormat.JSON:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)
