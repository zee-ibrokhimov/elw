from __future__ import annotations

import os

import pytest

#: Минимум, без которого Settings не собирается. Тесты не должны зависеть
#: от содержимого .env разработчика — иначе они «работают у меня».
_BASE_ENV = {
    "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
    "SECRET_ENC_KEY": "dGVzdC1rZXktZm9yLXVuaXQtdGVzdHMtb25seS0zMmI=",
    "TELEGRAM_BOT_TOKEN": "1:test",
    "ADMIN_TELEGRAM_IDS": "1",
    "OWNER_TZ": "Europe/Rome",
}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    """Собирает окружение с нуля и чистит кэш настроек.

    get_settings кэширован через lru_cache, поэтому без сброса второй тест
    получил бы конфиг первого.
    """
    from tutorsync.config import get_settings

    def _apply(**overrides: str):
        for key in list(os.environ):
            if key.isupper():
                monkeypatch.delenv(key, raising=False)
        for key, value in {**_BASE_ENV, **overrides}.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()
        return get_settings()

    yield _apply
    get_settings.cache_clear()
