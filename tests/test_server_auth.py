"""Tests for the per-user encrypted credential store and OAuth wiring."""

from __future__ import annotations

import pytest

from csv_to_clonehero.config import AppConfig
from csv_to_clonehero.database import Database
from csv_to_clonehero.server.auth import (
    EncryptedCredentialStore,
    build_oauth,
    user_workspace_key,
)
from csv_to_clonehero.server.security import TokenCipher, generate_fernet_key


@pytest.fixture
def cipher():
    return TokenCipher(generate_fernet_key())


@pytest.fixture
def db(config, cipher):
    database = Database(config.database_path)
    yield database
    database.close()


def test_user_workspace_key_is_globally_unique_per_user():
    assert user_workspace_key("alice") != user_workspace_key("bob")
    assert user_workspace_key("alice") == user_workspace_key("alice")


def test_encrypted_store_roundtrip(config, db, cipher):
    user = db.upsert_user(spotify_user_id="sp-user-1", display_name="Alice", scopes=["user-library-read"])
    store = EncryptedCredentialStore(config.database_path, cipher, int(user["id"]))
    store.set_password("csv-to-clonehero.spotify", f"user:{user['id']}", "refresh_token_value")
    stored = db.get_user_token(int(user["id"]))
    assert stored is not None
    # Plaintext must NOT be at rest.
    assert "refresh_token_value" not in (stored["encrypted_refresh_token"] or "")
    assert store.get_password("service", "user") == "refresh_token_value"


def test_encrypted_store_no_token_returns_none(config, db, cipher):
    user = db.upsert_user(spotify_user_id="sp-user-2")
    store = EncryptedCredentialStore(config.database_path, cipher, int(user["id"]))
    assert store.get_password("s", "u") is None


def test_encrypted_store_delete(config, db, cipher):
    user = db.upsert_user(spotify_user_id="sp-user-3")
    store = EncryptedCredentialStore(config.database_path, cipher, int(user["id"]))
    store.set_password("s", "u", "tok")
    store.delete_password("s", "u")
    assert db.get_user_token(int(user["id"])) is None


def test_users_are_isolated(config, db, cipher):
    alice = db.upsert_user(spotify_user_id="alice_id")
    bob = db.upsert_user(spotify_user_id="bob_id")
    alice_store = EncryptedCredentialStore(config.database_path, cipher, int(alice["id"]))
    alice_store.set_password("s", "u", "alice-token")
    bob_store = EncryptedCredentialStore(config.database_path, cipher, int(bob["id"]))
    assert bob_store.get_password("s", "u") is None


def test_sessions_lifecycle(config, db):
    import time

    user = db.upsert_user(spotify_user_id="session-user")
    uid = int(user["id"])
    row = db.create_session(uid, expires_at=time.time() + 3600)
    sid = row["session_id"]
    assert db.get_user_by_session(sid) is not None
    db.revoke_session(sid)
    assert db.get_user_by_session(sid) is None


def test_build_oauth_uses_encrypted_store(config, cipher, monkeypatch):
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "test-client-id")
    oauth = build_oauth(config, 1, cipher)
    # It should resolve to a per-user encrypted store without touching env.
    assert oauth.credential_username == "user:1"


def test_oauth_redirect_uses_public_base_url(monkeypatch):
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "c")
    monkeypatch.setenv("APP_BASE_URL", "https://riff.ryo-nd.com")
    config = AppConfig()
    assert config.spotify_redirect_uri == "https://riff.ryo-nd.com/api/auth/spotify/callback"


def test_oauth_redirect_loopback_when_no_public_url(monkeypatch):
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "c")
    monkeypatch.delenv("APP_BASE_URL", raising=False)
    config = AppConfig()
    assert config.spotify_redirect_uri.startswith("http://127.0.0.1:8765/")
