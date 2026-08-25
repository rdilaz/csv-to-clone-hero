"""Application secrets and at-rest encryption for the cloud web app.

Multi-user correctness requires that each user's Spotify refresh token lives
server-side (so background jobs can refresh on their behalf) but NEVER in the
OS keyring (which is per-login and breaks under multi-user/containers).

Design:
  * Every user's refresh token is stored in SQLite, encrypted with Fernet
    (AES-128-CBC + HMAC-SHA256, self-describing token that carries its IV).
  * The Fernet key comes from ``TOKEN_ENCRYPTION_KEY`` env / secret manager
    and is never written to disk or git.
  * The session signing key comes from ``SECRET_KEY`` env / secret manager.
"""

from __future__ import annotations

import base64
import os

from csv_to_clonehero.config import (
    AppConfig,
    SECRET_KEY_ENV,
    TOKEN_ENCRYPTION_KEY_ENV,
)


def generate_secret_key() -> str:
    """Return a url-safe random session signing key."""
    import secrets

    return secrets.token_urlsafe(48)


def generate_fernet_key() -> str:
    """Return a fresh Fernet key (url-safe base64 of 32 bytes)."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


class TokenCipher:
    """Fernet wrapper for encrypting/decrypting per-user refresh tokens."""

    def __init__(self, key: str | bytes | None = None) -> None:
        self.key = self._coerce_key(key or os.environ.get(TOKEN_ENCRYPTION_KEY_ENV, ""))
        try:
            from cryptography.fernet import Fernet  # type: ignore[import-not-found]

            self._fernet = Fernet(self.key.encode("ascii"))
        except (ImportError, ValueError) as exc:  # pragma: no cover - env-specific
            raise RuntimeError(
                "Token encryption is unavailable (install 'cryptography' and set "
                f"{TOKEN_ENCRYPTION_KEY_ENV} or pass a key)"
            ) from exc

    @staticmethod
    def _coerce_key(key: str | bytes) -> str:
        if isinstance(key, bytes):
            # Accept a raw 32-byte key directly.
            if len(key) == 32:
                return key
            try:
                key = key.decode("ascii")
            except UnicodeDecodeError as exc:
                raise ValueError("Fernet key must be ASCII") from exc
        value = str(key or "").strip()
        if not value:
            raise ValueError(f"{TOKEN_ENCRYPTION_KEY_ENV} is required")
        try:
            from cryptography.fernet import Fernet  # type: ignore[import-not-found]

            # Validate: a valid Fernet key is exactly 32 raw bytes (either a
            # str of 44 urlsafe base64 chars, possibly with padding, or bytes).
            candidate = value.encode("ascii")
            Fernet(candidate)
        except (ValueError, TypeError, UnicodeEncodeError) as exc:
            raise ValueError(
                f"{TOKEN_ENCRYPTION_KEY_ENV} must be a Fernet key (32 random "
                "bytes, base64-encoded); generate one with "
                "`python -m csv_to_clonehero keys`"
            ) from exc
        return value

    def encrypt(self, plaintext: str) -> str:
        if not isinstance(plaintext, str):
            raise TypeError("refresh token must be text")
        token = self._fernet.encrypt(plaintext.encode("utf-8"))
        return token.decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(str(ciphertext).encode("ascii")).decode("utf-8")
        except Exception as exc:  # noqa: BLE001 - broad per API contract
            raise ValueError("stored token could not be decrypted (key changed or corrupted)") from exc

    encrypt_token = encrypt
    decrypt_token = decrypt


def resolve_session_key(config: AppConfig) -> str:
    """Return the session signing key, preferring SECRET_KEY env.

    Falls back to a deterministic development key only when the server is not
    configured for production; production refuses to start without SECRET_KEY
    (enforced by ``config.server.require_secrets`` / the app factory).
    """
    env_key = os.environ.get(config.server.secret_key_env or SECRET_KEY_ENV, "").strip()
    if env_key:
        return env_key
    if config.server.enforce_secret_key:
        config.server.require_secrets()
    # Development-only fallback that still varies per base dir.
    import hashlib

    return hashlib.sha256(f"dev:{config.public_base_url or 'local'}".encode()).hexdigest()


def resolve_token_cipher(config: AppConfig) -> TokenCipher:
    key = config.crypto.load_key() or os.environ.get(TOKEN_ENCRYPTION_KEY_ENV, "")
    if not key:
        if config.server.enforce_secret_key:
            raise RuntimeError(
                f"{TOKEN_ENCRYPTION_KEY_ENV} is required for the multi-user server; "
                "generate one with `python -m csv_to_clonehero keys`"
            )
        # Development/test fallback only: tokens won't survive a restart.
        key = generate_fernet_key()
    return TokenCipher(key)