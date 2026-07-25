"""Проверки одноразового state.

Смысл этих тестов не в криптографии, а в единственном барьере, который стоит
перед публичным колбэком: если state можно подделать или переиспользовать
через сутки, посторонний подключит к сервису свой Google-аккаунт.
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


def test_fresh_state_passes(oauth):
    oauth.verify_state(oauth.make_state())


def test_expired_state_rejected(oauth):
    now = time.time()
    state = oauth.make_state(now=now)
    with pytest.raises(oauth.InvalidStateError, match="просрочена"):
        oauth.verify_state(state, now=now + oauth.STATE_TTL_SEC + 1)


def test_state_valid_right_before_expiry(oauth):
    now = time.time()
    state = oauth.make_state(now=now)
    oauth.verify_state(state, now=now + oauth.STATE_TTL_SEC - 1)


def test_tampered_signature_rejected(oauth):
    expires, _, signature = oauth.make_state().partition(".")
    forged = f"{expires}.{'0' * len(signature)}"
    with pytest.raises(oauth.InvalidStateError, match="подпись"):
        oauth.verify_state(forged)


def test_extended_deadline_rejected(oauth):
    """Продлить срок, не зная ключа, нельзя: он входит в подпись."""
    expires, _, signature = oauth.make_state().partition(".")
    forged = f"{int(expires) + 86400}.{signature}"
    with pytest.raises(oauth.InvalidStateError, match="подпись"):
        oauth.verify_state(forged)


@pytest.mark.parametrize("state", ["", "no-dot", "abc.def", ".", "1700000000."])
def test_malformed_state_rejected(oauth, state):
    with pytest.raises(oauth.InvalidStateError):
        oauth.verify_state(state)
