"""Расчёт пауз между повторами.

Проверяется не арифметика ради арифметики: если пауза не растёт, воркер во время
недоступности Google превращается в генератор запросов и выжигает суточную квоту
за несколько минут — после чего перестают работать и те операции, которые могли бы.
"""

from __future__ import annotations

from tutorsync.services.outbox import (
    BASE_BACKOFF_SEC,
    MAX_ATTEMPTS,
    MAX_BACKOFF_SEC,
    backoff_delay,
)


def test_first_retry_is_not_immediate():
    assert backoff_delay(1).total_seconds() == BASE_BACKOFF_SEC


def test_delay_doubles():
    assert backoff_delay(2).total_seconds() == BASE_BACKOFF_SEC * 2
    assert backoff_delay(3).total_seconds() == BASE_BACKOFF_SEC * 4


def test_delay_is_capped():
    assert backoff_delay(MAX_ATTEMPTS).total_seconds() <= MAX_BACKOFF_SEC
    assert backoff_delay(99).total_seconds() == MAX_BACKOFF_SEC


def test_delay_never_negative_or_zero():
    assert backoff_delay(0).total_seconds() > 0


def test_total_retry_window_is_hours_not_days():
    """Задание не должно тихо висеть в очереди сутки: алерт нужен в тот же день."""
    total = sum(backoff_delay(n).total_seconds() for n in range(1, MAX_ATTEMPTS + 1))
    assert 600 < total < 6 * 3600
