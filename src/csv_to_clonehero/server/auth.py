"""Multi-user Spotify OAuth support with per-user encrypted credential stores.

The single-user app stored one refresh token in the OS keyring (per-login, and
useless for background jobs / containers). The multi-user app instead binds a
:class:`EncryptedCredentialStore` to each user: refresh tokens live in SQLite,
encrypted with the server's Fernet key, keyed by ``user_id``. Storage follows
the existing ``CredentialStore`` protocol (``get_password`` / ``set_password`` /
``delete_password``) so it drops straight into the existing
:class:`csv_to_clonehero.spotify.SpotifyOAuth`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from csv_to_clonehero.config import AppConfig
from csv_to_clonehero.database import Database
from csv_to_clonehero.spotify import SpotifyClient, SpotifyOAuth, SpotifyOAuthConfig
from csv_to_clonehero.server.security import TokenCipher


class EncryptedCredentialStore:
    """A per-user, encrypted token store speaking the keyring API.

    The ``service``/``username`` arguments are ignored: identity is bound at
    construction to ``user_id`` so code paths cannot accidentally mix users.
    """

    def __init__(
        self,
        database_path: Path | str,
        cipher: TokenCipher,
        user_id: int,
    ) -> None:
        self.database_path = Path(database_path)
        self.cipher = cipher
        self.user_id = user_id

    def _db(self) -> Database:
        return Database(self.database_path)

    def get_password(self, service_name: str, username: str) -> str | None:
        db = self._db()
        try:
            row = db.get_user_token(self.user_id)
            if row is None or not row["encrypted_refresh_token"]:
                return None
            return self.cipher.decrypt(row["encrypted_refresh_token"])
        except (ValueError, Exception):  # noqa: BLE001 - surfaced as missing creds
            return None
        finally:
            db.close()

    def set_password(self, service_name: str, username: str, password: str) -> None:
        if not password:
            return
        db = self._db()
        try:
            db.set_user_token(self.user_id, self.cipher.encrypt(str(password)))
        finally:
            db.close()

    def delete_password(self, service_name: str, username: str) -> None:
        db = self._db()
        try:
            db.clear_user_token(self.user_id)
        finally:
            db.close()

    # Convenience aliases aligned with how caller code names things.
    get = get_password
    set = set_password
    delete = delete_password


def build_oauth(
    config: AppConfig,
    user_id: int,
    cipher: TokenCipher,
) -> SpotifyOAuth:
    store = EncryptedCredentialStore(config.database_path, cipher, user_id)
    config_model: SpotifyOAuthConfig = SpotifyOAuthConfig(
        client_id=config.spotify_client_id,
        scopes=config.spotify_scopes,
        redirect_port=config.spotify.redirect_port,
        redirect_path=config.spotify.redirect_path,
        redirect_uri=config.spotify_redirect_uri,
        api_base_url="https://api.spotify.com/v1",
    )
    return SpotifyOAuth(
        config=config_model,
        credential_store=store,
        credential_service="csv-to-clonehero.spotify",
        credential_username=f"user:{user_id}",
        timeout=float(config.network.timeout_seconds or 20.0),
        max_retries=int(config.network.retries or 3),
    )


def build_spotify_client(config: AppConfig, oauth: SpotifyOAuth | Any) -> SpotifyClient:
    return SpotifyClient(oauth=oauth, timeout=float(config.network.timeout_seconds or 20.0))


USER_WORKSPACE_BASE = "spotify"


def user_workspace_key(spotify_user_id: str, base: str = USER_WORKSPACE_BASE) -> str:
    """Global-unique discovery workspace key for one user (isolates state)."""
    return Database.workspace_key_for_user(spotify_user_id, base=base or USER_WORKSPACE_BASE)