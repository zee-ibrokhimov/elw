"""Портируемые типы колонок.

Postgres хранит timestamptz и возвращает aware-datetime, SQLite хранит строку
и возвращает naive. Если этого не выровнять, один и тот же код на двух базах
даёт разный результат сравнения дат — и вылезает это не в тестах, а в проде.
UtcDateTime приводит обе базы к одному контракту: на входе — любой aware
datetime, в базе — UTC, на выходе — aware datetime в UTC.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import DateTime, Dialect, TypeDecorator


class UtcDateTime(TypeDecorator[dt.datetime]):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: dt.datetime | None, dialect: Dialect) -> Any:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(
                "В базу можно писать только aware datetime. "
                f"Получено naive-значение {value!r} — где-то потерялся часовой пояс."
            )
        return value.astimezone(dt.UTC)

    def process_result_value(self, value: dt.datetime | None, dialect: Dialect) -> Any:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.UTC)
        return value.astimezone(dt.UTC)
