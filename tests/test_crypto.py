from __future__ import annotations

import pytest
from cryptography.fernet import Fernet


@pytest.fixture
def crypto(env):
    """Настройки со свежим ключом и сброшенным кэшем Fernet."""

    def _make(key: str | None = None):
        from tutorsync import crypto

        env(SECRET_ENC_KEY=key or Fernet.generate_key().decode())
        crypto.reset_cache()
        return crypto

    return _make


def test_roundtrip(crypto):
    mod = crypto()
    assert mod.decrypt(mod.encrypt("1//04refresh-token")) == "1//04refresh-token"


def test_ciphertext_differs_from_plaintext(crypto):
    mod = crypto()
    secret = "1//04refresh-token"
    assert secret not in mod.encrypt(secret)


def test_same_plaintext_gives_different_ciphertext(crypto):
    """Fernet подмешивает IV: одинаковые токены не выглядят одинаково в дампе."""
    mod = crypto()
    assert mod.encrypt("одно и то же") != mod.encrypt("одно и то же")


def test_other_key_cannot_read(crypto):
    """Смена SECRET_ENC_KEY должна давать внятную ошибку, а не мусор."""
    mod = crypto()
    ciphertext = mod.encrypt("refresh")

    mod = crypto()  # другой ключ
    with pytest.raises(mod.SecretUnreadableError):
        mod.decrypt(ciphertext)


def test_tampered_ciphertext_rejected(crypto):
    mod = crypto()
    ciphertext = mod.encrypt("refresh")
    broken = ciphertext[:-4] + ("aaaa" if not ciphertext.endswith("aaaa") else "bbbb")
    with pytest.raises(mod.SecretUnreadableError):
        mod.decrypt(broken)


def test_issued_at_returns_real_time(crypto):
    """Метка времени должна извлекаться, а не тихо возвращать None.

    На этом уже спотыкались: extract_timestamp — метод экземпляра, а не класса,
    и вызов через класс падал с TypeError. Проглоченный, он выключал проверку
    срока годности ссылки авторизации, ничем это не обозначая.
    """
    import time

    mod = crypto()
    created = mod.issued_at(mod.encrypt("что угодно"))

    assert created is not None
    assert abs(created - time.time()) < 5


def test_issued_at_on_garbage_is_none(crypto):
    mod = crypto()
    assert mod.issued_at("не токен вовсе") is None


def test_non_fernet_key_fails_loudly(crypto):
    """token_urlsafe(32) — самая вероятная ошибка при генерации ключа."""
    mod = crypto("this-is-not-a-fernet-key")
    with pytest.raises(RuntimeError, match="Fernet"):
        mod.encrypt("что угодно")
