"""Tests for at-rest token encryption and secret resolution."""

from __future__ import annotations

import pytest

from csv_to_clonehero.config import AppConfig
from csv_to_clonehero.server.security import (
    TokenCipher,
    generate_fernet_key,
    generate_secret_key,
    resolve_session_key,
    resolve_token_cipher,
)


def test_generate_secret_key_is_urlsafe_random():
    a, b = generate_secret_key(), generate_secret_key()
    assert a and a != b
    assert set(a) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")


def test_generate_fernet_key_is_valid():
    TokenCipher(generate_fernet_key())


def test_cipher_roundtrip():
    cipher = TokenCipher(generate_fernet_key())
    assert cipher.decrypt(cipher.encrypt("refresh-token")) == "refresh-token"
    assert cipher.encrypt("x") != cipher.encrypt("x")  # random IV each time


def test_cipher_rejects_invalid_key():
    with pytest.raises(ValueError):
        TokenCipher("not-a-valid-key")


def test_cipher_wrong_key_fails_to_decrypt():
    a = TokenCipher(generate_fernet_key())
    b = TokenCipher(generate_fernet_key())
    ciphertext = a.encrypt("secret")
    with pytest.raises(ValueError):
        b.decrypt(ciphertext)


def test_resolve_session_key_prefers_env(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "env-session-key" * 3)
    config = AppConfig()
    assert resolve_session_key(config) == "env-session-key" * 3


def test_resolve_session_key_dev_fallback():
    config = AppConfig()
    config.server.enforce_secret_key = False
    assert resolve_session_key(config)


def test_resolve_token_cipher_dev_fallback_is_usable():
    config = AppConfig()
    config.server.enforce_secret_key = False
    cipher = resolve_token_cipher(config)
    assert cipher.decrypt(cipher.encrypt("a")) == "a"


def test_resolve_token_cipher_requires_key_in_production(monkeypatch):
    monkeypatch.delenv("TOKEN_ENCRYPTION_KEY", raising=False)
    config = AppConfig()
    config.server.enforce_secret_key = True
    with pytest.raises(RuntimeError):
        resolve_token_cipher(config)