"""Декларативная база.

Явное соглашение об именах нужно, чтобы alembic мог генерировать обратимые
миграции: без него SQLite и Postgres дают констрейнтам разные автоимена,
и downgrade падает на «constraint does not exist».
"""

from __future__ import annotations

import datetime as dt
import enum
from typing import Any, TypeVar

import sqlalchemy as sa
from sqlalchemy import MetaData
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from tutorsync.db.types import UtcDateTime

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

E = TypeVar("E", bound=enum.Enum)


def enum_column(python_enum: type[E], **kwargs: Any) -> sa.Enum:
    """Enum как VARCHAR + CHECK, а не как нативный тип Postgres.

    Нативный ENUM в Postgres требует ALTER TYPE при каждом добавлении значения,
    что внутри транзакции миграции работает с оговорками. VARCHAR переносим
    между Postgres и SQLite и меняется обычной миграцией.
    """
    return sa.Enum(
        python_enum,
        native_enum=False,
        length=32,
        values_callable=lambda enum_cls: [member.value for member in enum_cls],
        **kwargs,
    )


#: JSON в Postgres — JSONB (индексируемый), в SQLite — обычный JSON-текст.
JsonType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    created_at: Mapped[dt.datetime] = mapped_column(
        UtcDateTime, server_default=sa.func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        UtcDateTime,
        server_default=sa.func.now(),
        onupdate=lambda: dt.datetime.now(dt.UTC),
        nullable=False,
    )
