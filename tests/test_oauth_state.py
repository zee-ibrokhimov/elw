"""Проверки одноразового state.

Смысл этих тестов не в криптографии, а в двух вещах сразу.

Первая — единственный барьер перед публичным колбэком: если state можно
подделать или переиспользовать через сутки, посторонний подключит к сервису
свой Google-аккаунт.

Вторая — доставка code_verifier. Он генерируется при построении ссылки, а нужен
в другом HTTP-запросе, где объект Flow создаётся заново. Ровно на этом
авторизация уже ломалась один раз: Google отвечал «Missing code verifier».
"""

from __future__ import annotations

import time

import pytest


@pytest.fixture
def oauth(env):
    env()
    from tutorsync import crypto
    from tutorsync.gcal import oauth as mod

    crypto.reset_cache()
    return mod


def test_verifier_survives_the_round_trip(oauth):
    verifier = oauth.make_code_verifier()
    assert oauth.verify_state(oauth._pack_state(verifier)) == verifier


def test_verifier_is_not_readable_in_the_url(oauth):
    """state уезжает в адресную строку и оседает в истории браузера."""
    verifier = oauth.make_code_verifier()
    assert verifier not in oauth._pack_state(verifier)


def test_verifier_matches_rfc_7636(oauth):
    verifier = oauth.make_code_verifier()
    assert 43 <= len(verifier) <= 128
    assert set(verifier) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    )


def test_each_link_gets_its_own_verifier(oauth):
    assert oauth.make_code_verifier() != oauth.make_code_verifier()


def test_fresh_state_passes(oauth):
    oauth.verify_state(oauth._pack_state("verifier"))


def test_expired_state_rejected(oauth):
    state = oauth._pack_state("verifier")
    with pytest.raises(oauth.InvalidStateError, match="просрочена"):
        oauth.verify_state(state, now=time.time() + oauth.STATE_TTL_SEC + 5)


def test_state_valid_right_before_expiry(oauth):
    state = oauth._pack_state("verifier")
    oauth.verify_state(state, now=time.time() + oauth.STATE_TTL_SEC - 5)


def test_tampered_state_rejected(oauth):
    state = oauth._pack_state("verifier")
    broken = state[:-6] + ("AAAAAA" if not state.endswith("AAAAAA") else "BBBBBB")
    with pytest.raises(oauth.InvalidStateError):
        oauth.verify_state(broken)


def test_state_from_another_key_rejected(oauth, env):
    """Чужой сервис со своим ключом не выпишет пропуск в наш колбэк."""
    from cryptography.fernet import Fernet

    from tutorsync import crypto

    state = oauth._pack_state("verifier")
    env(SECRET_ENC_KEY=Fernet.generate_key().decode())
    crypto.reset_cache()

    with pytest.raises(oauth.InvalidStateError):
        oauth.verify_state(state)


@pytest.mark.parametrize("state", ["", "not-a-token", "abc.def", "1700000000.deadbeef"])
def test_malformed_state_rejected(oauth, state):
    with pytest.raises(oauth.InvalidStateError):
        oauth.verify_state(state)
