"""Spotify OAuth and Web API integration primitives.

This module deliberately contains no Flask routes, UI code, or dependency on
the optional ``keyring`` package.  Applications can provide an httpx-like
client and a keyring-like credential store, which keeps OAuth and API behavior
easy to test and leaves persistence decisions to the caller.

The library importer only retains playlist URLs and artwork as navigation
metadata.  Track records do not carry playlist artwork or other presentation
fields, so they can be stored by the database layer without coupling it to a
web view.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from types import TracebackType
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence
from urllib.parse import quote, urlencode, urljoin, urlsplit

import httpx

from csv_to_clonehero.normalization import normalize_artist, normalize_title


ACCOUNTS_BASE_URL = "https://accounts.spotify.com"
API_BASE_URL = "https://api.spotify.com/v1"
AUTHORIZE_PATH = "/authorize"
TOKEN_PATH = "/api/token"
# Reuse the existing local web app port by default; callers may pass a
# dynamically reserved port to begin_authorization().
DEFAULT_REDIRECT_PORT = 8765
DEFAULT_REDIRECT_PATH = "/api/auth/spotify/callback"
DEFAULT_CREDENTIAL_SERVICE = "csv-to-clonehero.spotify"
SPOTIFY_CLIENT_ID_ENV = "SPOTIFY_CLIENT_ID"

# These are the only scopes needed to enumerate private/collaborative
# playlists and the current user's saved tracks.
DEFAULT_SCOPES = (
    "user-read-private",
    "playlist-read-private",
    "playlist-read-collaborative",
    "user-library-read",
)
MAX_PAGE_SIZE = 50


class CredentialStore(Protocol):
    """Small subset of the keyring API required by :class:`SpotifyOAuth`."""

    def get_password(self, service_name: str, username: str) -> str | None:
        ...

    def set_password(self, service_name: str, username: str, password: str) -> None:
        ...

    def delete_password(self, service_name: str, username: str) -> None:
        ...


class HttpClient(Protocol):
    """The request method used by the Spotify classes.

    ``httpx.Client`` satisfies this protocol.  A small fake implementing
    ``request`` is sufficient for offline tests; the implementation also
    accepts clients exposing only ``get`` and ``post`` for convenience.
    """

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        ...


class InMemoryCredentialStore:
    """A keyring-compatible in-memory store for tests and non-persistent use."""

    def __init__(self, initial: Mapping[tuple[str, str], str] | None = None) -> None:
        self.values: dict[tuple[str, str], str] = dict(initial or {})

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        self.values.pop((service_name, username), None)


# Both names are useful in tests and make it clear that no real keyring is
# contacted by default.
MemoryCredentialStore = InMemoryCredentialStore
FakeKeyring = InMemoryCredentialStore


_QUERY_SECRET_RE = re.compile(
    r"(?i)(\b(?:access_token|refresh_token|client_secret|code_verifier|authorization|code)\s*=\s*)([^&\s]+)"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[^\s,]+")


def _redact(value: object, secrets_to_hide: Iterable[str] = ()) -> str:
    """Return a bounded message with common OAuth values removed."""
    message = str(value)
    for secret in sorted({s for s in secrets_to_hide if s}, key=len, reverse=True):
        message = message.replace(secret, "[REDACTED]")
    message = _QUERY_SECRET_RE.sub(r"\1[REDACTED]", message)
    message = _BEARER_RE.sub("Bearer [REDACTED]", message)
    return message[:500]


class SpotifyError(Exception):
    """Base class for safe, reportable Spotify failures."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
        secrets_to_hide: Iterable[str] = (),
    ) -> None:
        self.status_code = status_code
        # ``status`` mirrors the source adapter's error shape and is useful to
        # callers that handle multiple external services.
        self.status = status_code
        self.retry_after = retry_after
        self.message = _redact(message, secrets_to_hide)
        super().__init__(self.message)


class SpotifyAuthError(SpotifyError):
    """The user must authenticate again or stored credentials are invalid."""


class SpotifyOAuthError(SpotifyAuthError):
    """OAuth state, PKCE, authorization-code, or token endpoint failure."""


class SpotifyCredentialError(SpotifyAuthError):
    """The injected credential store could not read or write credentials."""


class SpotifyNetworkError(SpotifyError):
    """The injected HTTP client could not reach Spotify."""


class SpotifyAPIError(SpotifyError):
    """Spotify returned an unsuccessful API response."""


class SpotifyPermissionError(SpotifyAPIError):
    """Spotify rejected a request because of permissions or app access."""


class SpotifyRateLimitError(SpotifyAPIError):
    """Spotify continued to rate-limit a request after the retry budget."""


class SpotifyCancellationError(Exception):
    """A caller cancelled Spotify work between bounded network requests."""


# Alternative spelling used by some callers.
SpotifyApiError = SpotifyAPIError


def _normalise_scopes(scopes: Iterable[str] | str) -> tuple[str, ...]:
    if isinstance(scopes, str):
        values = scopes.split()
    else:
        values = [str(scope).strip() for scope in scopes]
    return tuple(dict.fromkeys(scope for scope in values if scope))


def _join_url(base: str, path: str) -> str:
    return base.rstrip("/") + "/" + path.lstrip("/")


def build_loopback_redirect_uri(
    port: int = DEFAULT_REDIRECT_PORT,
    path: str = DEFAULT_REDIRECT_PATH,
) -> str:
    """Build the explicit Spotify-supported loopback redirect URI.

    ``localhost`` is intentionally not accepted here.  Spotify's loopback
    redirect guidance requires an IP literal, and using ``127.0.0.1`` also
    avoids hostname resolution ambiguity.
    """
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("redirect port must be an integer from 1 through 65535")
    if not path.startswith("/") or "?" in path or "#" in path:
        raise ValueError("redirect path must be an absolute path without query or fragment")
    return f"http://127.0.0.1:{port}{path}"


def _validate_loopback_redirect_uri(redirect_uri: str) -> str:
    parsed = urlsplit(redirect_uri)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("redirect URI has an invalid port") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith("/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("redirect URI must use http://127.0.0.1:<port> without query or fragment")
    return redirect_uri


def _validate_redirect_uri(redirect_uri: str) -> str:
    """Allow Spotify's loopback callback or a configured HTTPS callback."""
    parsed = urlsplit(redirect_uri)
    if parsed.scheme == "http" and parsed.hostname == "127.0.0.1":
        return _validate_loopback_redirect_uri(redirect_uri)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
    ):
        raise ValueError("redirect URI must be an HTTPS URL without query or fragment")
    return redirect_uri


def generate_state() -> str:
    """Generate a cryptographically random OAuth CSRF state value."""
    return secrets.token_urlsafe(32)


def generate_code_verifier() -> str:
    """Generate an RFC 7636 verifier using a cryptographic RNG."""
    # 64 random bytes produce a URL-safe verifier in the RFC 7636 43-128
    # character range without introducing punctuation outside the allowed set.
    verifier = secrets.token_urlsafe(64)
    if not 43 <= len(verifier) <= 128:
        raise RuntimeError("could not create a valid PKCE verifier")
    return verifier


def code_challenge_for_verifier(verifier: str) -> str:
    """Return the RFC 7636 S256 challenge for ``verifier``."""
    if not isinstance(verifier, str) or not 43 <= len(verifier) <= 128:
        raise ValueError("PKCE verifier must be 43-128 characters")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@dataclass(frozen=True)
class PKCEPair:
    """A PKCE verifier and its S256 challenge."""

    verifier: str = field(repr=False)
    challenge: str

    @classmethod
    def create(cls, verifier: str | None = None) -> "PKCEPair":
        actual_verifier = verifier or generate_code_verifier()
        return cls(actual_verifier, code_challenge_for_verifier(actual_verifier))

    @property
    def code_verifier(self) -> str:
        return self.verifier

    @property
    def code_challenge(self) -> str:
        return self.challenge


PKCE = PKCEPair
create_pkce_pair = PKCEPair.create
generate_pkce_verifier = generate_code_verifier
generate_pkce_challenge = code_challenge_for_verifier


@dataclass(frozen=True)
class SpotifyOAuthConfig:
    """Configuration shared by the OAuth and API clients."""

    client_id: str
    client_secret: str | None = field(default=None, repr=False)
    scopes: tuple[str, ...] = DEFAULT_SCOPES
    redirect_port: int = DEFAULT_REDIRECT_PORT
    redirect_path: str = DEFAULT_REDIRECT_PATH
    redirect_uri: str | None = None
    accounts_base_url: str = ACCOUNTS_BASE_URL
    api_base_url: str = API_BASE_URL

    def __post_init__(self) -> None:
        if not self.client_id or not self.client_id.strip():
            raise ValueError("Spotify client_id is required")
        object.__setattr__(self, "scopes", _normalise_scopes(self.scopes))
        if self.redirect_uri is None:
            object.__setattr__(
                self,
                "redirect_uri",
                build_loopback_redirect_uri(self.redirect_port, self.redirect_path),
            )
        else:
            _validate_redirect_uri(self.redirect_uri)
        if not self.accounts_base_url.startswith(("http://", "https://")):
            raise ValueError("accounts_base_url must be an absolute URL")
        if not self.api_base_url.startswith(("http://", "https://")):
            raise ValueError("api_base_url must be an absolute URL")

    @property
    def authorization_endpoint(self) -> str:
        return _join_url(self.accounts_base_url, AUTHORIZE_PATH)

    @property
    def token_endpoint(self) -> str:
        return _join_url(self.accounts_base_url, TOKEN_PATH)

    def redirect_uri_for(self, port: int | None = None) -> str:
        if urlsplit(self.redirect_uri or "").scheme == "https":
            return self.redirect_uri or ""
        if port is None:
            # __post_init__ guarantees this is not None.
            return self.redirect_uri or build_loopback_redirect_uri(
                self.redirect_port, self.redirect_path
            )
        return build_loopback_redirect_uri(port, self.redirect_path)


OAuthConfig = SpotifyOAuthConfig
SpotifySettings = SpotifyOAuthConfig


@dataclass(frozen=True)
class SpotifyAuthorizationRequest:
    """State required to send a user to Spotify and validate its callback."""

    url: str = field(repr=False)
    state: str = field(repr=False)
    pkce: PKCEPair = field(repr=False)
    redirect_uri: str

    @property
    def authorization_url(self) -> str:
        return self.url

    @property
    def code_verifier(self) -> str:
        return self.pkce.verifier

    @property
    def code_challenge(self) -> str:
        return self.pkce.challenge


AuthorizationRequest = SpotifyAuthorizationRequest


def build_authorization_url(
    client_id: str,
    redirect_uri: str,
    scope: Iterable[str] | str,
    state: str,
    code_challenge: str,
    *,
    authorization_endpoint: str = _join_url(ACCOUNTS_BASE_URL, AUTHORIZE_PATH),
) -> str:
    """Build a Spotify Authorization Code with PKCE URL."""
    if not client_id:
        raise ValueError("Spotify client_id is required")
    if not state:
        raise ValueError("OAuth state is required")
    if not code_challenge:
        raise ValueError("PKCE code challenge is required")
    _validate_redirect_uri(redirect_uri)
    query = urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": " ".join(_normalise_scopes(scope)),
            "code_challenge_method": "S256",
            "code_challenge": code_challenge,
            "state": state,
        }
    )
    separator = "&" if "?" in authorization_endpoint else "?"
    return f"{authorization_endpoint}{separator}{query}"


@dataclass(frozen=True)
class SpotifyToken:
    """OAuth token data with an absolute expiry time."""

    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    expires_at: float | None = None
    token_type: str = "Bearer"
    scope: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.access_token:
            raise ValueError("Spotify access token is required")
        object.__setattr__(self, "scope", _normalise_scopes(self.scope))

    @property
    def expired(self) -> bool:
        return self.is_expired()

    def is_expired(self, now: float | None = None, leeway: float = 0.0) -> bool:
        return self.expires_at is not None and self.expires_at <= (time.time() if now is None else now) + leeway

    @classmethod
    def from_response(
        cls,
        payload: Mapping[str, Any],
        *,
        now: float | None = None,
        previous: "SpotifyToken | None" = None,
    ) -> "SpotifyToken":
        access_token = str(payload.get("access_token") or "")
        if not access_token:
            raise SpotifyOAuthError("Spotify did not return an access token")
        current_time = time.time() if now is None else now
        expires_at_raw = payload.get("expires_at")
        if expires_at_raw is None and payload.get("expires_in") is not None:
            try:
                expires_at_raw = current_time + float(payload["expires_in"])
            except (TypeError, ValueError) as exc:
                raise SpotifyOAuthError("Spotify returned an invalid token expiry") from exc
        expires_at: float | None
        if expires_at_raw is None:
            expires_at = previous.expires_at if previous else None
        else:
            try:
                expires_at = float(expires_at_raw)
            except (TypeError, ValueError) as exc:
                raise SpotifyOAuthError("Spotify returned an invalid token expiry") from exc
        refresh_token = payload.get("refresh_token") or (previous.refresh_token if previous else None)
        scope_value = payload.get("scope")
        if scope_value is None and previous:
            scopes = previous.scope
        else:
            scopes = _normalise_scopes(scope_value or ())
        return cls(
            access_token=access_token,
            refresh_token=str(refresh_token) if refresh_token else None,
            expires_at=expires_at,
            token_type=str(payload.get("token_type") or (previous.token_type if previous else "Bearer")),
            scope=scopes,
        )

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "access_token": self.access_token,
            "token_type": self.token_type,
            "scope": " ".join(self.scope),
        }
        if self.refresh_token:
            data["refresh_token"] = self.refresh_token
        if self.expires_at is not None:
            data["expires_at"] = self.expires_at
        return data


TokenSet = SpotifyToken
SpotifyTokens = SpotifyToken


def _store_get(store: object, service: str, username: str) -> str | None:
    getter = getattr(store, "get_password", None) or getattr(store, "get", None)
    if not callable(getter):
        raise TypeError("credential store must provide get_password(service, username)")
    value = getter(service, username)
    return value if value is None or isinstance(value, str) else str(value)


def _store_set(store: object, service: str, username: str, value: str) -> None:
    setter = getattr(store, "set_password", None) or getattr(store, "set", None)
    if not callable(setter):
        raise TypeError("credential store must provide set_password(service, username, value)")
    setter(service, username, value)


def _store_delete(store: object, service: str, username: str) -> None:
    deleter = getattr(store, "delete_password", None) or getattr(store, "delete", None)
    if callable(deleter):
        deleter(service, username)


def _request_with_client(
    client: HttpClient,
    method: str,
    url: str,
    **kwargs: Any,
) -> Any:
    request = getattr(client, "request", None)
    if callable(request):
        return request(method, url, **kwargs)
    method_call = getattr(client, method.lower(), None)
    if not callable(method_call):
        raise TypeError("HTTP client must provide request(), get(), or post()")
    return method_call(url, **kwargs)


def _response_status(response: Any) -> int:
    try:
        return int(response.status_code)
    except (AttributeError, TypeError, ValueError) as exc:
        raise SpotifyAPIError("Spotify returned an invalid HTTP response") from exc


def _response_headers(response: Any) -> Mapping[str, str]:
    headers = getattr(response, "headers", {})
    return headers if hasattr(headers, "items") else {}


def _header_value(response: Any, name: str) -> str | None:
    wanted = name.lower()
    for key, value in _response_headers(response).items():
        if str(key).lower() == wanted:
            return str(value)
    return None


def _response_json(response: Any) -> Any:
    value = getattr(response, "json", None)
    if not callable(value):
        raise ValueError("response has no JSON method")
    return value()


def parse_retry_after(value: str | None, *, now: float | None = None) -> float | None:
    """Parse numeric or HTTP-date Retry-After values into seconds."""
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except (TypeError, ValueError):
        pass
    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        current = datetime.now(timezone.utc).timestamp() if now is None else now
        return max(0.0, retry_at.timestamp() - current)
    except (TypeError, ValueError, OverflowError):
        return None


def _safe_http_detail(response: Any, secrets_to_hide: Iterable[str] = ()) -> str:
    try:
        payload = _response_json(response)
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, Mapping):
        return ""
    for key in ("error_description", "message", "error"):
        value = payload.get(key)
        if value:
            return _redact(value, secrets_to_hide)
    return ""


class SpotifyOAuth:
    """PKCE authorization and token storage for Spotify."""

    def __init__(
        self,
        config: SpotifyOAuthConfig | None = None,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        scopes: Iterable[str] | str = DEFAULT_SCOPES,
        redirect_port: int = DEFAULT_REDIRECT_PORT,
        redirect_path: str = DEFAULT_REDIRECT_PATH,
        redirect_uri: str | None = None,
        accounts_base_url: str = ACCOUNTS_BASE_URL,
        api_base_url: str = API_BASE_URL,
        credential_store: CredentialStore | None = None,
        keyring: CredentialStore | None = None,
        token_store: CredentialStore | None = None,
        credential_service: str = DEFAULT_CREDENTIAL_SERVICE,
        credential_username: str | None = None,
        http_client: HttpClient | None = None,
        client: HttpClient | None = None,
        timeout: float = 20.0,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        max_retries: int = 3,
    ) -> None:
        if config is not None and client_id is not None:
            raise ValueError("provide config or client_id, not both")
        stores = [store for store in (credential_store, keyring, token_store) if store is not None]
        if len(stores) > 1:
            raise ValueError("provide only one credential store")
        clients = [candidate for candidate in (http_client, client) if candidate is not None]
        if len(clients) > 1:
            raise ValueError("provide only one HTTP client")
        self.config = config or SpotifyOAuthConfig(
            client_id=client_id or os.environ.get(SPOTIFY_CLIENT_ID_ENV, ""),
            client_secret=client_secret,
            scopes=_normalise_scopes(scopes),
            redirect_port=redirect_port,
            redirect_path=redirect_path,
            redirect_uri=redirect_uri,
            accounts_base_url=accounts_base_url,
            api_base_url=api_base_url,
        )
        self._store: CredentialStore | None = stores[0] if stores else InMemoryCredentialStore()
        self.credential_service = credential_service
        self.credential_username = credential_username or self.config.client_id
        self._http: HttpClient
        self._owns_http = not clients
        if clients:
            self._http = clients[0]
        else:
            self._http = httpx.Client(timeout=timeout, follow_redirects=True)
        self._sleep = sleeper
        self._clock = clock
        self.max_retries = max(0, int(max_retries))
        self._token: SpotifyToken | None = None
        self._pending_request: SpotifyAuthorizationRequest | None = None

    @property
    def http_client(self) -> HttpClient:
        return self._http

    @property
    def token(self) -> SpotifyToken | None:
        return self._token

    def close(self) -> None:
        if self._owns_http:
            close = getattr(self._http, "close", None)
            if callable(close):
                close()

    def __enter__(self) -> "SpotifyOAuth":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def begin_authorization(
        self,
        *,
        port: int | None = None,
        state: str | None = None,
        code_verifier: str | None = None,
    ) -> SpotifyAuthorizationRequest:
        pkce = PKCEPair.create(code_verifier)
        actual_state = state or generate_state()
        redirect = self.config.redirect_uri_for(port)
        request = SpotifyAuthorizationRequest(
            url=build_authorization_url(
                self.config.client_id,
                redirect,
                self.config.scopes,
                actual_state,
                pkce.challenge,
                authorization_endpoint=self.config.authorization_endpoint,
            ),
            state=actual_state,
            pkce=pkce,
            redirect_uri=redirect,
        )
        self._pending_request = request
        return request

    def authorization_url(
        self,
        *,
        port: int | None = None,
        state: str | None = None,
        code_verifier: str | None = None,
    ) -> str:
        """Create and remember a PKCE authorization request, returning its URL."""
        return self.begin_authorization(
            port=port,
            state=state,
            code_verifier=code_verifier,
        ).url

    build_authorization_url = authorization_url

    def _request_token_endpoint(self, data: Mapping[str, str]) -> Mapping[str, Any]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        for attempt in range(self.max_retries + 1):
            try:
                response = _request_with_client(
                    self._http,
                    "POST",
                    self.config.token_endpoint,
                    data=dict(data),
                    headers=headers,
                )
            except (httpx.HTTPError, OSError, TimeoutError) as exc:
                raise SpotifyNetworkError("Spotify token request failed") from exc
            status = _response_status(response)
            if status == 429:
                retry_after = parse_retry_after(
                    _header_value(response, "Retry-After"),
                    now=self._clock(),
                )
                if attempt < self.max_retries:
                    self._sleep(retry_after if retry_after is not None else min(2.0**attempt, 30.0))
                    continue
                raise SpotifyRateLimitError(
                    "Spotify token request was rate-limited",
                    status_code=status,
                    retry_after=retry_after,
                )
            if status == 403:
                raise SpotifyPermissionError(
                    "Spotify rejected the token request (HTTP 403). Verify the "
                    "app and redirect URI are allowlisted in the Spotify "
                    "Developer Dashboard and that the requested scopes are allowed.",
                    status_code=status,
                )
            if not 200 <= status < 300:
                detail = _safe_http_detail(
                    response,
                    (
                        self.config.client_secret or "",
                        str(data.get("code") or ""),
                        str(data.get("code_verifier") or ""),
                        str(data.get("refresh_token") or ""),
                    ),
                )
                suffix = f": {detail}" if detail else ""
                raise SpotifyOAuthError(
                    f"Spotify token request failed (HTTP {status}){suffix}",
                    status_code=status,
                    secrets_to_hide=(
                        self.config.client_secret or "",
                        str(data.get("code") or ""),
                        str(data.get("code_verifier") or ""),
                        str(data.get("refresh_token") or ""),
                    ),
                )
            try:
                payload = _response_json(response)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise SpotifyOAuthError("Spotify token response was not valid JSON") from exc
            if not isinstance(payload, Mapping):
                raise SpotifyOAuthError("Spotify token response was not an object")
            return payload
        raise AssertionError("unreachable token retry loop")

    def load_refresh_token(self) -> str | None:
        """Read only the refresh token from the injected secure store."""
        if self._store is None:
            return None
        try:
            raw = _store_get(self._store, self.credential_service, self.credential_username)
        except Exception as exc:
            raise SpotifyCredentialError("Could not read stored Spotify credentials") from exc
        if not raw:
            return None
        # A refresh token is stored as an opaque value.  Accepting the old
        # JSON shape here is harmless for callers migrating an in-memory fake,
        # but new writes never persist access tokens.
        if raw.lstrip().startswith("{"):
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise SpotifyCredentialError("Stored Spotify credentials are invalid; sign in again") from exc
            if not isinstance(payload, Mapping):
                raise SpotifyCredentialError("Stored Spotify credentials are invalid; sign in again")
            raw = str(payload.get("refresh_token") or "")
        return raw or None

    def load_token(self) -> SpotifyToken | None:
        """Return the in-memory access token, if this process has one."""
        return self._token

    def save_token(self, token: SpotifyToken) -> SpotifyToken:
        if not isinstance(token, SpotifyToken):
            raise TypeError("token must be a SpotifyToken")
        if self._store is not None and token.refresh_token:
            try:
                _store_set(
                    self._store,
                    self.credential_service,
                    self.credential_username,
                    token.refresh_token,
                )
            except Exception as exc:
                raise SpotifyCredentialError("Could not store Spotify credentials") from exc
        self._token = token
        return token

    def clear_token(self) -> None:
        self._token = None
        if self._store is not None:
            try:
                _store_delete(self._store, self.credential_service, self.credential_username)
            except Exception as exc:
                raise SpotifyCredentialError("Could not clear stored Spotify credentials") from exc

    logout = clear_token
    forget_spotify = clear_token

    def exchange_code(
        self,
        code: str,
        authorization_request: SpotifyAuthorizationRequest | None = None,
        *,
        returned_state: str | None = None,
        state: str | None = None,
        code_verifier: str | None = None,
        redirect_uri: str | None = None,
    ) -> SpotifyToken:
        """Exchange an authorization code after validating the OAuth state."""
        if not isinstance(code, str) or not code.strip():
            raise SpotifyOAuthError("Spotify authorization code is required")
        request = authorization_request or self._pending_request
        callback_state = returned_state if returned_state is not None else state
        if request is not None:
            if not isinstance(callback_state, str) or not secrets.compare_digest(request.state, callback_state):
                raise SpotifyOAuthError("OAuth state validation failed; start sign-in again")
        verifier = code_verifier or (request.code_verifier if request else None)
        if not verifier:
            raise SpotifyOAuthError("PKCE verifier is required to exchange the authorization code")
        redirect = redirect_uri or (request.redirect_uri if request else self.config.redirect_uri)
        _validate_redirect_uri(redirect)
        data: dict[str, str] = {
            "client_id": self.config.client_id,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect,
            "code_verifier": verifier,
        }
        if self.config.client_secret:
            data["client_secret"] = self.config.client_secret
        payload = self._request_token_endpoint(data)
        token = SpotifyToken.from_response(payload, now=self._clock())
        self.save_token(token)
        self._pending_request = None
        return token

    def refresh_access_token(self, refresh_token: str | None = None) -> SpotifyToken:
        """Refresh a token, preserving a rotated-token value when omitted."""
        previous = self._token
        actual_refresh_token = (
            refresh_token
            or (previous.refresh_token if previous else None)
            or self.load_refresh_token()
        )
        if not actual_refresh_token:
            raise SpotifyAuthError("Spotify sign-in is required; no refresh token is available")
        data: dict[str, str] = {
            "client_id": self.config.client_id,
            "grant_type": "refresh_token",
            "refresh_token": actual_refresh_token,
        }
        if self.config.client_secret:
            data["client_secret"] = self.config.client_secret
        payload = self._request_token_endpoint(data)
        token = SpotifyToken.from_response(payload, now=self._clock(), previous=previous)
        return self.save_token(token)

    refresh_token = refresh_access_token

    def access_token(self, *, force_refresh: bool = False) -> SpotifyToken:
        token = self._token
        if token is None:
            stored_refresh_token = self.load_refresh_token()
            if stored_refresh_token:
                return self.refresh_access_token(stored_refresh_token)
            raise SpotifyAuthError("Spotify sign-in is required")
        if force_refresh or token.is_expired(self._clock()):
            return self.refresh_access_token(token.refresh_token)
        return token

    get_access_token = access_token


class LibraryItemStatus(str, Enum):
    """Terminal outcome for one playlist or liked-track item."""

    IMPORTED = "imported"
    INCLUDED = "imported"
    DUPLICATE = "duplicate"
    SKIPPED_NULL = "skipped_null"
    SKIPPED_LOCAL = "skipped_local"
    SKIPPED_EPISODE = "skipped_episode"
    UNAVAILABLE = "unavailable"
    SKIPPED_UNAVAILABLE = "unavailable"
    INACCESSIBLE = "inaccessible"
    ERROR = "error"


SpotifyItemStatus = LibraryItemStatus
LibraryStatus = LibraryItemStatus


@dataclass(frozen=True)
class PlaylistNavigation:
    """Playlist identity and navigation-only metadata."""

    id: str
    name: str
    url: str = ""
    artwork_url: str = ""
    access_status: str = "accessible"
    access_reason: str = ""

    @property
    def playlist_id(self) -> str:
        return self.id

    @property
    def href(self) -> str:
        return self.url

    @property
    def artwork(self) -> str:
        return self.artwork_url


@dataclass(frozen=True)
class SpotifyPlaylist:
    """Accessible playlist metadata, including its Spotify owner."""

    id: str
    name: str
    url: str = ""
    artwork_url: str = ""
    total_tracks: int | None = None
    access_status: str = "accessible"
    access_reason: str = ""
    owner_id: str = ""
    owner_name: str = ""
    is_owned: bool | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SpotifyPlaylist | None":
        playlist_id = str(payload.get("id") or "").strip()
        if not playlist_id:
            return None
        external_urls = payload.get("external_urls")
        if not isinstance(external_urls, Mapping):
            external_urls = {}
        url = str(external_urls.get("spotify") or payload.get("href") or "")
        images = payload.get("images")
        artwork_url = ""
        if isinstance(images, Sequence) and not isinstance(images, (str, bytes)):
            for image in images:
                if isinstance(image, Mapping) and image.get("url"):
                    artwork_url = str(image["url"])
                    break
        total_tracks: int | None = None
        items = payload.get("items")
        tracks = payload.get("tracks")
        count = items.get("total") if isinstance(items, Mapping) else tracks.get("total") if isinstance(tracks, Mapping) else None
        if count is not None:
            try:
                total_tracks = int(count)
            except (TypeError, ValueError):
                total_tracks = None
        owner = payload.get("owner") if isinstance(payload.get("owner"), Mapping) else {}
        owner_id = str(payload.get("owner_id") or owner.get("id") or "")
        owner_name = str(payload.get("owner_name") or owner.get("display_name") or owner.get("name") or "")
        raw_owned = payload.get("is_owned")
        is_owned = bool(raw_owned) if raw_owned is not None else None
        return cls(
            id=playlist_id,
            name=str(payload.get("name") or ""),
            url=url,
            artwork_url=artwork_url,
            total_tracks=total_tracks,
            access_status=str(payload.get("access_status") or "accessible"),
            access_reason=str(payload.get("access_reason") or ""),
            owner_id=owner_id,
            owner_name=owner_name,
            is_owned=is_owned,
        )

    @property
    def navigation(self) -> PlaylistNavigation:
        return PlaylistNavigation(
            self.id,
            self.name,
            self.url,
            self.artwork_url,
            self.access_status,
            self.access_reason,
        )

    @property
    def playlist_id(self) -> str:
        return self.id


PlaylistMetadata = SpotifyPlaylist


@dataclass
class SpotifyLibraryRecord:
    """One accessible, deduplicated Spotify track."""

    spotify_id: str
    title: str
    artists: list[str]
    album: str = ""
    album_id: str = ""
    playlists: list[PlaylistNavigation] = field(default_factory=list)
    liked: bool = False
    added_at: str | None = None
    status: LibraryItemStatus = LibraryItemStatus.INCLUDED
    duration_ms: int | None = None
    isrc: str = ""
    url: str = ""

    @property
    def track_id(self) -> str:
        return self.spotify_id

    @property
    def id(self) -> str:
        return self.spotify_id

    @property
    def name(self) -> str:
        return self.title

    @property
    def artist(self) -> str:
        return self.artists[0] if self.artists else ""

    @property
    def primary_artist(self) -> str:
        return self.artist

    @property
    def playlist_ids(self) -> list[str]:
        return [playlist.id for playlist in self.playlists]

    @property
    def playlist_names(self) -> list[str]:
        return [playlist.name for playlist in self.playlists]


SpotifyTrackRecord = SpotifyLibraryRecord
SpotifyTrack = SpotifyLibraryRecord


@dataclass(frozen=True)
class LibraryItemOutcome:
    """A terminal parse outcome retained for status/count reporting."""

    status: LibraryItemStatus
    source: str
    playlist: PlaylistNavigation | None = None
    spotify_id: str = ""
    title: str = ""
    reason: str = ""

    @property
    def terminal_status(self) -> LibraryItemStatus:
        return self.status

    @property
    def playlist_id(self) -> str:
        return self.playlist.id if self.playlist else ""


ItemOutcome = LibraryItemOutcome


@dataclass
class LibraryCounts:
    """Stable counters for a completed library parse."""

    playlist_count: int = 0
    playlist_item_count: int = 0
    liked_item_count: int = 0
    included_count: int = 0
    duplicate_count: int = 0
    skipped_null_count: int = 0
    skipped_local_count: int = 0
    skipped_episode_count: int = 0
    skipped_unavailable_count: int = 0
    inaccessible_count: int = 0
    error_count: int = 0

    @property
    def total_item_count(self) -> int:
        return self.playlist_item_count + self.liked_item_count

    @property
    def skipped_count(self) -> int:
        return (
            self.skipped_null_count
            + self.skipped_local_count
            + self.skipped_episode_count
            + self.skipped_unavailable_count
        )

    @property
    def unique_count(self) -> int:
        return self.included_count

    @property
    def imported_count(self) -> int:
        return self.included_count

    @property
    def unavailable_count(self) -> int:
        return self.skipped_unavailable_count

    @property
    def terminal_counts(self) -> dict[str, int]:
        return {
            LibraryItemStatus.INCLUDED.value: self.included_count,
            LibraryItemStatus.DUPLICATE.value: self.duplicate_count,
            LibraryItemStatus.SKIPPED_NULL.value: self.skipped_null_count,
            LibraryItemStatus.SKIPPED_LOCAL.value: self.skipped_local_count,
            LibraryItemStatus.SKIPPED_EPISODE.value: self.skipped_episode_count,
            LibraryItemStatus.UNAVAILABLE.value: self.skipped_unavailable_count,
            LibraryItemStatus.INACCESSIBLE.value: self.inaccessible_count,
            LibraryItemStatus.ERROR.value: self.error_count,
        }

    @property
    def status_counts(self) -> dict[str, int]:
        return self.terminal_counts


@dataclass
class LibraryImportResult:
    """Parsed library data plus every item's terminal status."""

    records: list[SpotifyLibraryRecord]
    playlists: list[SpotifyPlaylist] = field(default_factory=list)
    outcomes: list[LibraryItemOutcome] = field(default_factory=list)
    counts: LibraryCounts = field(default_factory=LibraryCounts)

    @property
    def tracks(self) -> list[SpotifyLibraryRecord]:
        return self.records

    @property
    def items(self) -> list[SpotifyLibraryRecord]:
        return self.records

    @property
    def status_counts(self) -> dict[str, int]:
        return self.counts.status_counts

    @property
    def terminal_counts(self) -> dict[str, int]:
        return self.counts.terminal_counts

    @property
    def skipped_counts(self) -> dict[str, int]:
        return {
            status: count
            for status, count in self.status_counts.items()
            if status.startswith("skipped_") or status == LibraryItemStatus.UNAVAILABLE.value
        }

    @property
    def included_count(self) -> int:
        return self.counts.included_count

    @property
    def skipped_count(self) -> int:
        return self.counts.skipped_count


LibraryResult = LibraryImportResult


def _coerce_playlist(value: SpotifyPlaylist | PlaylistNavigation | Mapping[str, Any] | None) -> SpotifyPlaylist | None:
    if isinstance(value, SpotifyPlaylist):
        return value
    if isinstance(value, PlaylistNavigation):
        return SpotifyPlaylist(value.id, value.name, value.url, value.artwork_url)
    if isinstance(value, Mapping):
        return SpotifyPlaylist.from_payload(value)
    return None


def _as_playlist_navigation(
    value: SpotifyPlaylist | PlaylistNavigation | Mapping[str, Any] | None,
) -> PlaylistNavigation | None:
    playlist = _coerce_playlist(value)
    return playlist.navigation if playlist else None


def _outcome(
    status: LibraryItemStatus,
    source: str,
    playlist: PlaylistNavigation | None,
    *,
    track_id: str = "",
    title: str = "",
    reason: str = "",
) -> LibraryItemOutcome:
    return LibraryItemOutcome(status, source, playlist, track_id, title, reason)


def _item_track(item: Any) -> tuple[Mapping[str, Any] | None, LibraryItemStatus | None, str]:
    if item is None:
        return None, LibraryItemStatus.SKIPPED_NULL, "item is null"
    if not isinstance(item, Mapping):
        return None, LibraryItemStatus.SKIPPED_UNAVAILABLE, "item is not an object"
    if "track" in item:
        track = item.get("track")
    else:
        # Accept raw track dictionaries in addition to Web API item wrappers;
        # this makes the parser useful with cached/database payloads.
        track = item
    if track is None:
        return None, LibraryItemStatus.SKIPPED_NULL, "track is null"
    if not isinstance(track, Mapping):
        return None, LibraryItemStatus.SKIPPED_UNAVAILABLE, "track is not an object"
    local = bool(item.get("is_local")) or bool(track.get("is_local"))
    uri = str(track.get("uri") or "")
    if local or uri.startswith("spotify:local:"):
        return track, LibraryItemStatus.SKIPPED_LOCAL, "local track"
    track_type = str(track.get("type") or "").lower()
    if track_type == "episode" or "episode" in item and item.get("episode") is not None:
        return track, LibraryItemStatus.SKIPPED_EPISODE, "episode item"
    if track_type and track_type != "track":
        return track, LibraryItemStatus.SKIPPED_UNAVAILABLE, "item is not a playable track"
    if track.get("is_playable") is False:
        return track, LibraryItemStatus.SKIPPED_UNAVAILABLE, "track is unavailable"
    markets = track.get("available_markets")
    if isinstance(markets, Sequence) and not isinstance(markets, (str, bytes)) and not markets:
        return track, LibraryItemStatus.SKIPPED_UNAVAILABLE, "track has no available markets"
    track_id = str(track.get("id") or "").strip()
    title = str(track.get("name") or "").strip()
    artists = track.get("artists")
    if not track_id or not title or not isinstance(artists, Sequence) or isinstance(artists, (str, bytes)):
        return track, LibraryItemStatus.SKIPPED_UNAVAILABLE, "track is missing playable metadata"
    if not any(isinstance(artist, Mapping) and str(artist.get("name") or "").strip() for artist in artists):
        return track, LibraryItemStatus.SKIPPED_UNAVAILABLE, "track has no artist metadata"
    return track, None, ""


def _record_from_item(
    item: Mapping[str, Any],
    playlist: PlaylistNavigation | None,
    liked: bool,
) -> SpotifyLibraryRecord:
    track, status, reason = _item_track(item)
    if track is None or status is not None:
        raise ValueError(reason or "item is not an accessible track")
    artists = [
        str(artist.get("name") or "").strip()
        for artist in track.get("artists", ())
        if isinstance(artist, Mapping) and str(artist.get("name") or "").strip()
    ]
    album = track.get("album")
    album_name = str(album.get("name") or "") if isinstance(album, Mapping) else ""
    album_id = str(album.get("id") or "") if isinstance(album, Mapping) else ""
    external_urls = track.get("external_urls")
    url = str(external_urls.get("spotify") or "") if isinstance(external_urls, Mapping) else ""
    external_ids = track.get("external_ids")
    isrc = str(external_ids.get("isrc") or "") if isinstance(external_ids, Mapping) else ""
    duration_ms: int | None = None
    if track.get("duration_ms") is not None:
        try:
            duration_ms = int(track["duration_ms"])
        except (TypeError, ValueError):
            duration_ms = None
    added_at = item.get("added_at") if isinstance(item, Mapping) else None
    record = SpotifyLibraryRecord(
        spotify_id=str(track["id"]),
        title=str(track["name"]),
        artists=artists,
        album=album_name,
        album_id=album_id,
        playlists=[playlist] if playlist else [],
        liked=liked,
        added_at=str(added_at) if added_at else None,
        duration_ms=duration_ms,
        isrc=isrc,
        url=url,
    )
    return record


def _merge_record_metadata(existing: SpotifyLibraryRecord, incoming: SpotifyLibraryRecord) -> None:
    """Fill gaps in first-seen metadata without replacing stable identity."""
    if not existing.title and incoming.title:
        existing.title = incoming.title
    if not existing.artists and incoming.artists:
        existing.artists = list(incoming.artists)
    if not existing.album and incoming.album:
        existing.album = incoming.album
    if not existing.album_id and incoming.album_id:
        existing.album_id = incoming.album_id
    if existing.duration_ms is None and incoming.duration_ms is not None:
        existing.duration_ms = incoming.duration_ms
    if not existing.isrc and incoming.isrc:
        existing.isrc = incoming.isrc
    if not existing.url and incoming.url:
        existing.url = incoming.url
    if not existing.added_at and incoming.added_at:
        existing.added_at = incoming.added_at


def _playlist_batches(
    playlist_items: Mapping[str, Iterable[Any]]
    | Iterable[tuple[SpotifyPlaylist | PlaylistNavigation | Mapping[str, Any], Iterable[Any]]]
    | Iterable[Mapping[str, Any]],
) -> Iterable[tuple[SpotifyPlaylist | PlaylistNavigation | Mapping[str, Any] | None, Iterable[Any]]]:
    if isinstance(playlist_items, Mapping):
        for playlist_id, items in playlist_items.items():
            yield SpotifyPlaylist(str(playlist_id), str(playlist_id)), items
        return
    for batch in playlist_items:
        if isinstance(batch, Mapping):
            playlist = batch.get("playlist")
            items = batch.get("items", ())
            yield playlist, items
        else:
            try:
                playlist, items = batch
            except (TypeError, ValueError) as exc:
                raise ValueError("playlist batches must contain (playlist, items)") from exc
            yield playlist, items


def parse_library(
    playlist_items: Mapping[str, Iterable[Any]]
    | Iterable[tuple[SpotifyPlaylist | PlaylistNavigation | Mapping[str, Any], Iterable[Any]]]
    | Iterable[Mapping[str, Any]] = (),
    liked_items: Iterable[Any] = (),
    *,
    playlists: Iterable[SpotifyPlaylist | Mapping[str, Any]] = (),
) -> LibraryImportResult:
    """Parse, deduplicate, and account for playlist and liked-track items.

    The input accepts raw Spotify item payloads.  Every input item produces a
    terminal :class:`LibraryItemOutcome`, including null, local, episode,
    unavailable, and duplicate items.  Only first-seen accessible tracks are
    emitted in ``records``.
    """
    records_by_id: dict[str, SpotifyLibraryRecord] = {}
    records: list[SpotifyLibraryRecord] = []
    outcomes: list[LibraryItemOutcome] = []
    playlist_models: list[SpotifyPlaylist] = []
    seen_playlist_ids: set[str] = set()
    playlist_item_count = 0
    liked_item_count = 0

    def add_playlist_model(value: SpotifyPlaylist | PlaylistNavigation | Mapping[str, Any] | None) -> SpotifyPlaylist | None:
        model = _coerce_playlist(value)
        if model and model.id not in seen_playlist_ids:
            seen_playlist_ids.add(model.id)
            playlist_models.append(model)
        return model

    for playlist in playlists:
        add_playlist_model(playlist)

    for playlist_value, items in _playlist_batches(playlist_items):
        playlist_model = add_playlist_model(playlist_value)
        navigation = playlist_model.navigation if playlist_model else _as_playlist_navigation(playlist_value)
        for item in items:
            playlist_item_count += 1
            track, terminal, reason = _item_track(item)
            if terminal is not None:
                track_id = str(track.get("id") or "") if isinstance(track, Mapping) else ""
                title = str(track.get("name") or "") if isinstance(track, Mapping) else ""
                outcomes.append(
                    _outcome(
                        terminal,
                        "playlist",
                        navigation,
                        track_id=track_id,
                        title=title,
                        reason=reason,
                    )
                )
                continue
            assert isinstance(item, Mapping)
            record = _record_from_item(item, navigation, False)
            existing = records_by_id.get(record.spotify_id)
            if existing is not None:
                _merge_record_metadata(existing, record)
                if navigation and navigation.id not in {entry.id for entry in existing.playlists}:
                    existing.playlists.append(navigation)
                outcomes.append(
                    _outcome(
                        LibraryItemStatus.DUPLICATE,
                        "playlist",
                        navigation,
                        track_id=record.spotify_id,
                        title=record.title,
                        reason="track already included",
                    )
                )
                continue
            records_by_id[record.spotify_id] = record
            records.append(record)
            outcomes.append(
                _outcome(
                    LibraryItemStatus.INCLUDED,
                    "playlist",
                    navigation,
                    track_id=record.spotify_id,
                    title=record.title,
                )
            )

    for item in liked_items:
        liked_item_count += 1
        track, terminal, reason = _item_track(item)
        if terminal is not None:
            track_id = str(track.get("id") or "") if isinstance(track, Mapping) else ""
            title = str(track.get("name") or "") if isinstance(track, Mapping) else ""
            outcomes.append(
                _outcome(
                    terminal,
                    "liked",
                    None,
                    track_id=track_id,
                    title=title,
                    reason=reason,
                )
            )
            continue
        assert isinstance(item, Mapping)
        record = _record_from_item(item, None, True)
        existing = records_by_id.get(record.spotify_id)
        if existing is not None:
            _merge_record_metadata(existing, record)
            existing.liked = True
            outcomes.append(
                _outcome(
                    LibraryItemStatus.DUPLICATE,
                    "liked",
                    None,
                    track_id=record.spotify_id,
                    title=record.title,
                    reason="track already included",
                )
            )
            continue
        records_by_id[record.spotify_id] = record
        records.append(record)
        outcomes.append(
            _outcome(
                LibraryItemStatus.INCLUDED,
                "liked",
                None,
                track_id=record.spotify_id,
                title=record.title,
            )
        )

    counts = LibraryCounts(
        playlist_count=len(playlist_models),
        playlist_item_count=playlist_item_count,
        liked_item_count=liked_item_count,
    )
    for outcome in outcomes:
        if outcome.status is LibraryItemStatus.INCLUDED:
            counts.included_count += 1
        elif outcome.status is LibraryItemStatus.DUPLICATE:
            counts.duplicate_count += 1
        elif outcome.status is LibraryItemStatus.SKIPPED_NULL:
            counts.skipped_null_count += 1
        elif outcome.status is LibraryItemStatus.SKIPPED_LOCAL:
            counts.skipped_local_count += 1
        elif outcome.status is LibraryItemStatus.SKIPPED_EPISODE:
            counts.skipped_episode_count += 1
        elif outcome.status is LibraryItemStatus.SKIPPED_UNAVAILABLE:
            counts.skipped_unavailable_count += 1
        elif outcome.status is LibraryItemStatus.INACCESSIBLE:
            counts.inaccessible_count += 1
        elif outcome.status is LibraryItemStatus.ERROR:
            counts.error_count += 1
    return LibraryImportResult(records, playlist_models, outcomes, counts)


class SpotifyLibraryParser:
    """Stateless parser facade for callers that do not need the HTTP client."""

    def parse(
        self,
        playlist_items: Mapping[str, Iterable[Any]]
        | Iterable[tuple[SpotifyPlaylist | PlaylistNavigation | Mapping[str, Any], Iterable[Any]]]
        | Iterable[Mapping[str, Any]] = (),
        liked_items: Iterable[Any] = (),
        *,
        playlists: Iterable[SpotifyPlaylist | Mapping[str, Any]] = (),
    ) -> LibraryImportResult:
        return parse_library(playlist_items, liked_items, playlists=playlists)

    parse_library = parse


def parse_playlist(payload: Mapping[str, Any]) -> SpotifyPlaylist | None:
    """Parse one playlist payload while retaining only navigation metadata."""
    return SpotifyPlaylist.from_payload(payload)


class SpotifyClient:
    """Authenticated Spotify Web API client and library importer."""

    def __init__(
        self,
        oauth: SpotifyOAuth | None = None,
        *,
        config: SpotifyOAuthConfig | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        scopes: Iterable[str] | str = DEFAULT_SCOPES,
        credential_store: CredentialStore | None = None,
        keyring: CredentialStore | None = None,
        token_store: CredentialStore | None = None,
        http_client: HttpClient | None = None,
        client: HttpClient | None = None,
        timeout: float = 20.0,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        max_retries: int = 3,
        page_size: int = MAX_PAGE_SIZE,
    ) -> None:
        if oauth is not None and any(
            value is not None
            for value in (config, client_id, client_secret, credential_store, keyring, token_store, http_client, client)
        ):
            raise ValueError("oauth cannot be combined with client or credential configuration")
        if page_size < 1 or page_size > MAX_PAGE_SIZE:
            raise ValueError(f"page_size must be from 1 through {MAX_PAGE_SIZE}")
        self._oauth = oauth or SpotifyOAuth(
            config=config,
            client_id=client_id,
            client_secret=client_secret,
            scopes=scopes,
            credential_store=credential_store,
            keyring=keyring,
            token_store=token_store,
            http_client=http_client,
            client=client,
            timeout=timeout,
            sleeper=sleeper,
            clock=clock,
            max_retries=max_retries,
        )
        self.config = self._oauth.config
        self._sleep = sleeper if oauth is None else self._oauth._sleep
        self._max_retries = max(0, int(max_retries)) if oauth is None else self._oauth.max_retries
        self.page_size = page_size

    @property
    def oauth(self) -> SpotifyOAuth:
        return self._oauth

    @property
    def http_client(self) -> HttpClient:
        return self._oauth.http_client

    def close(self) -> None:
        self._oauth.close()

    def __enter__(self) -> "SpotifyClient":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def begin_authorization(self, **kwargs: Any) -> SpotifyAuthorizationRequest:
        return self._oauth.begin_authorization(**kwargs)

    def authorization_url(self, **kwargs: Any) -> str:
        return self._oauth.authorization_url(**kwargs)

    def exchange_code(self, *args: Any, **kwargs: Any) -> SpotifyToken:
        return self._oauth.exchange_code(*args, **kwargs)

    def _api_url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return _join_url(self.config.api_base_url, path)

    def _safe_endpoint(self, method: str, url: str) -> str:
        parsed = urlsplit(url)
        return f"{method.upper()} {parsed.path or '/'}"

    @staticmethod
    def _check_cancelled(cancellation_event: Any | None) -> None:
        if cancellation_event is not None and cancellation_event.is_set():
            raise SpotifyCancellationError("Spotify operation cancelled")

    def _wait_for_retry(self, delay: float, cancellation_event: Any | None) -> None:
        self._check_cancelled(cancellation_event)
        if cancellation_event is None:
            self._sleep(delay)
            return
        wait = getattr(cancellation_event, "wait", None)
        if callable(wait):
            if wait(delay):
                raise SpotifyCancellationError("Spotify operation cancelled")
            self._check_cancelled(cancellation_event)
            return
        remaining = max(0.0, delay)
        while remaining > 0:
            interval = min(remaining, 0.1)
            self._sleep(interval)
            remaining -= interval
            self._check_cancelled(cancellation_event)

    def _permission_error(self, status: int) -> SpotifyPermissionError:
        scopes = ", ".join(self.config.scopes) or "the configured Spotify scopes"
        return SpotifyPermissionError(
            "Spotify returned HTTP 403. Verify the app and redirect URI are "
            "allowlisted in the Spotify Developer Dashboard, and request the "
            f"required scopes: {scopes}.",
            status_code=status,
        )

    def _api_error(
        self,
        response: Any,
        method: str,
        url: str,
        *,
        secrets_to_hide: Iterable[str] = (),
    ) -> None:
        status = _response_status(response)
        if status == 403:
            raise self._permission_error(status)
        if status == 401:
            raise SpotifyAuthError(
                "Spotify rejected the access token (HTTP 401); sign in again if refresh fails",
                status_code=status,
            )
        if status == 429:
            retry_after = parse_retry_after(
                _header_value(response, "Retry-After"),
                now=self._oauth._clock(),
            )
            raise SpotifyRateLimitError(
                f"Spotify rate limit exceeded for {self._safe_endpoint(method, url)}",
                status_code=status,
                retry_after=retry_after,
            )
        detail = _safe_http_detail(response, secrets_to_hide)
        suffix = f": {detail}" if detail else ""
        raise SpotifyAPIError(
            f"Spotify API request failed for {self._safe_endpoint(method, url)} (HTTP {status}){suffix}",
            status_code=status,
        )

    def _request_json(
        self,
        method: str,
        path_or_url: str,
        *,
        params: Mapping[str, Any] | None = None,
        cancellation_event: Any | None = None,
    ) -> Any:
        url = self._api_url(path_or_url)
        refreshed_after_401 = False
        rate_attempt = 0
        while True:
            self._check_cancelled(cancellation_event)
            token = self._oauth.access_token()
            headers = {
                "Accept": "application/json",
                "Authorization": f"{token.token_type} {token.access_token}",
            }
            request_kwargs: dict[str, Any] = {"headers": headers}
            if params is not None:
                request_kwargs["params"] = dict(params)
            try:
                response = _request_with_client(
                    self.http_client,
                    method,
                    url,
                    **request_kwargs,
                )
            except (httpx.HTTPError, OSError, TimeoutError) as exc:
                raise SpotifyNetworkError(
                    f"Spotify network request failed for {self._safe_endpoint(method, url)}"
                ) from exc
            status = _response_status(response)
            if status == 401 and not refreshed_after_401:
                self._check_cancelled(cancellation_event)
                refreshed_after_401 = True
                try:
                    self._oauth.access_token(force_refresh=True)
                except SpotifyError:
                    raise
                continue
            if status == 429 and rate_attempt < self._max_retries:
                retry_after = parse_retry_after(
                    _header_value(response, "Retry-After"),
                    now=self._oauth._clock(),
                )
                self._wait_for_retry(
                    retry_after if retry_after is not None else min(2.0**rate_attempt, 30.0),
                    cancellation_event,
                )
                rate_attempt += 1
                continue
            if not 200 <= status < 300:
                self._api_error(
                    response,
                    method,
                    url,
                    secrets_to_hide=(
                        token.access_token,
                        token.refresh_token or "",
                        self.config.client_secret or "",
                    ),
                )
            if status == 204:
                return {}
            try:
                return _response_json(response)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise SpotifyAPIError(
                    f"Spotify returned invalid JSON for {self._safe_endpoint(method, url)}",
                    status_code=status,
                ) from exc

    def _paginate(
        self,
        path: str,
        *,
        limit: int | None = None,
        query: Mapping[str, Any] | None = None,
        cancellation_event: Any | None = None,
        on_page: Any | None = None,
    ) -> list[Any]:
        page_limit = self.page_size if limit is None else limit
        if page_limit < 1 or page_limit > MAX_PAGE_SIZE:
            raise ValueError(f"limit must be from 1 through {MAX_PAGE_SIZE}")
        url = self._api_url(path)
        offset = 0
        base_query = dict(query or {})
        params: Mapping[str, Any] | None = {**base_query, "limit": page_limit, "offset": offset}
        items: list[Any] = []
        seen_urls: set[str] = set()
        followed_next = False
        for _ in range(1000):
            self._check_cancelled(cancellation_event)
            payload = self._request_json(
                "GET",
                url,
                params=params,
                cancellation_event=cancellation_event,
            )
            if not isinstance(payload, Mapping):
                raise SpotifyAPIError("Spotify pagination response was not an object")
            page = payload.get("items")
            if not isinstance(page, Sequence) or isinstance(page, (str, bytes)):
                raise SpotifyAPIError("Spotify pagination response had no items list")
            # Preserve null item slots so the library parser can report them
            # instead of silently changing Spotify's terminal counts.
            items.extend(page)
            if callable(on_page):
                on_page(len(items), payload.get("total"))
            next_url = payload.get("next")
            if next_url:
                next_value = urljoin(url, str(next_url))
                if next_value in seen_urls or next_value == url:
                    raise SpotifyAPIError("Spotify pagination returned a repeated next URL")
                seen_urls.add(url)
                url = next_value
                params = None
                followed_next = True
                continue
            total = payload.get("total")
            if not followed_next and total is not None and offset + len(page) < int(total) and page:
                offset += len(page)
                url = self._api_url(path)
                params = {**base_query, "limit": page_limit, "offset": offset}
                continue
            return items
        raise SpotifyAPIError("Spotify pagination exceeded the safety limit")

    def list_playlists(self, *, limit: int | None = None) -> list[SpotifyPlaylist]:
        """Fetch all accessible playlists using ``/v1/me/playlists`` pages."""
        payloads = self._paginate("/me/playlists", limit=limit)
        result: list[SpotifyPlaylist] = []
        for payload in payloads:
            if not isinstance(payload, Mapping):
                continue
            playlist = SpotifyPlaylist.from_payload(payload)
            if playlist is not None:
                result.append(playlist)
        return result

    get_playlists = list_playlists
    fetch_playlists = list_playlists

    def get_playlist_items(self, playlist_id: str, *, limit: int | None = None, on_page: Any | None = None) -> list[Any]:
        """Fetch all items from the current playlist using Spotify's items endpoint."""
        if not str(playlist_id).strip():
            raise ValueError("playlist_id is required")
        encoded_id = quote(str(playlist_id), safe="")
        return self._paginate(f"/playlists/{encoded_id}/items", limit=limit, on_page=on_page)

    playlist_items = get_playlist_items
    fetch_playlist_items = get_playlist_items

    def get_liked_tracks(self, *, limit: int | None = None, on_page: Any | None = None) -> list[Any]:
        """Fetch all saved tracks from ``/v1/me/tracks``."""
        return self._paginate("/me/tracks", limit=limit, on_page=on_page)

    def get_saved_tracks_count(self) -> int:
        """Fetch the saved-track total without downloading the library."""
        payload = self._request_json(
            "GET",
            self._api_url("/me/tracks"),
            params={"limit": 1, "offset": 0},
        )
        if not isinstance(payload, Mapping):
            raise SpotifyAPIError("Spotify saved tracks response was not an object")
        try:
            return max(0, int(payload.get("total", 0)))
        except (TypeError, ValueError) as exc:
            raise SpotifyAPIError("Spotify saved tracks response had an invalid total") from exc

    saved_tracks_count = get_saved_tracks_count

    def search_artist(
        self,
        artist_name: str,
        *,
        cancellation_event: Any | None = None,
    ) -> Mapping[str, Any] | None:
        name = str(artist_name or "").strip()
        if not name:
            raise ValueError("artist_name is required")
        self._check_cancelled(cancellation_event)
        payload = self._request_json(
            "GET",
            self._api_url("/search"),
            params={"q": name, "type": "artist", "limit": 10},
            cancellation_event=cancellation_event,
        )
        artists = payload.get("artists", {}) if isinstance(payload, Mapping) else {}
        items = artists.get("items", []) if isinstance(artists, Mapping) else []
        valid = [item for item in items if isinstance(item, Mapping) and item.get("id")]
        folded = name.casefold()
        return next(
            (
                item
                for item in valid
                if str(item.get("name") or "").strip().casefold() == folded
            ),
            valid[0] if valid else None,
        )

    def get_artist_discography(
        self,
        artist_name: str,
        *,
        cancellation_event: Any | None = None,
    ) -> dict[str, Any]:
        self._check_cancelled(cancellation_event)
        artist = self.search_artist(artist_name, cancellation_event=cancellation_event)
        if artist is None:
            return {"artist_name": artist_name, "artist_id": "", "albums": 0, "tracks": []}
        artist_id = str(artist.get("id") or "")
        encoded_artist = quote(artist_id, safe="")
        albums = self._paginate(
            f"/artists/{encoded_artist}/albums",
            query={"include_groups": "album,single"},
            cancellation_event=cancellation_event,
        )
        album_rows = {str(album.get("id")): album for album in albums if isinstance(album, Mapping) and album.get("id")}
        tracks: dict[tuple[str, str], dict[str, Any]] = {}
        for album_id, album in album_rows.items():
            self._check_cancelled(cancellation_event)
            encoded_album = quote(album_id, safe="")
            for track in self._paginate(
                f"/albums/{encoded_album}/tracks",
                cancellation_event=cancellation_event,
            ):
                if not isinstance(track, Mapping):
                    continue
                artists = [value for value in track.get("artists", []) if isinstance(value, Mapping)]
                if not artists or str(artists[0].get("id") or "") != artist_id:
                    continue
                title = str(track.get("name") or "").strip()
                if not title:
                    continue
                display_artist = str(artist.get("name") or artist_name)
                key = (normalize_artist(display_artist), normalize_title(title))
                tracks.setdefault(
                    key,
                    {
                        "spotify_id": str(track.get("id") or ""),
                        "title": title,
                        "artist": display_artist,
                        "artist_id": artist_id,
                        "album": str(album.get("name") or ""),
                        "album_id": album_id,
                        "duration_ms": track.get("duration_ms"),
                    },
                )
        return {
            "artist_name": str(artist.get("name") or artist_name),
            "artist_id": artist_id,
            "albums": len(album_rows),
            "tracks": list(tracks.values()),
        }

    liked_tracks = get_liked_tracks
    fetch_liked_tracks = get_liked_tracks

    def fetch_library(
        self,
        *,
        include_liked: bool = True,
        playlists: Sequence[SpotifyPlaylist] | None = None,
        on_progress: Any | None = None,
    ) -> LibraryImportResult:
        """Fetch and parse accessible playlists and optionally saved tracks."""
        accessible_playlists = list(playlists) if playlists is not None else self.list_playlists()
        playlist_batches: list[tuple[SpotifyPlaylist, list[Any]]] = []
        inaccessible: list[LibraryItemOutcome] = []
        for index, playlist in enumerate(accessible_playlists):
            try:
                if callable(on_progress):
                    on_progress({"phase": "playlist", "playlist_index": index + 1, "playlist_total": len(accessible_playlists), "playlist_name": playlist.name, "playlist_total_items": playlist.total_tracks})
                items = self.get_playlist_items(
                    playlist.id,
                    on_page=(lambda count, total, index=index, playlist=playlist: on_progress({"phase": "playlist", "playlist_index": index + 1, "playlist_total": len(accessible_playlists), "playlist_name": playlist.name, "playlist_items_read": count, "playlist_total_items": total or playlist.total_tracks}) if callable(on_progress) else None),
                )
                playlist_batches.append((playlist, items))
                if callable(on_progress):
                    on_progress({"phase": "playlist", "playlist_index": index + 1, "playlist_total": len(accessible_playlists), "playlist_name": playlist.name, "playlist_items_read": len(items), "playlist_total_items": playlist.total_tracks})
            except SpotifyPermissionError as exc:
                # A playlist can be listed while its current items endpoint is
                # inaccessible (for example, a followed private playlist).
                # Keep the folder visible and retain the actionable terminal
                # state instead of aborting the whole library sync.
                inaccessible_playlist = replace(
                    playlist,
                    access_status=LibraryItemStatus.INACCESSIBLE.value,
                    access_reason=str(exc),
                )
                accessible_playlists[index] = inaccessible_playlist
                inaccessible.append(
                    LibraryItemOutcome(
                        status=LibraryItemStatus.INACCESSIBLE,
                        source="playlist",
                        playlist=inaccessible_playlist.navigation,
                        reason=str(exc),
                    )
                )
            except SpotifyAPIError as exc:
                # Other per-playlist API failures are also isolated so a
                # transient or stale playlist cannot hide the rest of the
                # account's library.
                inaccessible_playlist = replace(
                    playlist,
                    access_status=LibraryItemStatus.ERROR.value,
                    access_reason=str(exc),
                )
                accessible_playlists[index] = inaccessible_playlist
                inaccessible.append(
                    LibraryItemOutcome(
                        status=LibraryItemStatus.ERROR,
                        source="playlist",
                        playlist=inaccessible_playlist.navigation,
                        reason=str(exc),
                    )
                )
        if callable(on_progress) and include_liked:
            on_progress({"phase": "liked", "liked_total_items": None, "liked_items_read": 0})
        liked = self.get_liked_tracks(
            on_page=(lambda count, total: on_progress({"phase": "liked", "liked_items_read": count, "liked_total_items": total}) if callable(on_progress) else None),
        ) if include_liked else []
        if callable(on_progress) and include_liked:
            on_progress({"phase": "liked", "liked_total_items": len(liked), "liked_items_read": len(liked)})
        result = parse_library(playlist_batches, liked, playlists=accessible_playlists)
        if inaccessible:
            result.outcomes.extend(inaccessible)
            result.counts.inaccessible_count += sum(
                item.status is LibraryItemStatus.INACCESSIBLE for item in inaccessible
            )
            result.counts.error_count += sum(
                item.status is LibraryItemStatus.ERROR for item in inaccessible
            )
        return result

    import_library = fetch_library


SpotifyAPIClient = SpotifyClient
SpotifyApiClient = SpotifyClient
SpotifyService = SpotifyClient


__all__ = [
    "ACCOUNTS_BASE_URL",
    "API_BASE_URL",
    "AUTHORIZE_PATH",
    "AuthorizationRequest",
    "CredentialStore",
    "DEFAULT_CREDENTIAL_SERVICE",
    "DEFAULT_REDIRECT_PATH",
    "DEFAULT_REDIRECT_PORT",
    "DEFAULT_SCOPES",
    "FakeKeyring",
    "HttpClient",
    "InMemoryCredentialStore",
    "ItemOutcome",
    "LibraryCounts",
    "LibraryImportResult",
    "LibraryItemOutcome",
    "LibraryItemStatus",
    "LibraryResult",
    "LibraryStatus",
    "MAX_PAGE_SIZE",
    "MemoryCredentialStore",
    "OAuthConfig",
    "PKCE",
    "PKCEPair",
    "PlaylistMetadata",
    "PlaylistNavigation",
    "SpotifyAPIClient",
    "SpotifyAPIError",
    "SpotifyApiClient",
    "SpotifyApiError",
    "SpotifyAuthError",
    "SpotifyAuthorizationRequest",
    "SpotifyClient",
    "SpotifyCredentialError",
    "SpotifyError",
    "SpotifyItemStatus",
    "SpotifyLibraryParser",
    "SpotifyLibraryRecord",
    "SpotifyNetworkError",
    "SpotifyOAuth",
    "SpotifyOAuthConfig",
    "SpotifyOAuthError",
    "SpotifyPermissionError",
    "SpotifyPlaylist",
    "SpotifyRateLimitError",
    "SpotifyService",
    "SpotifySettings",
    "SpotifyToken",
    "SpotifyTokens",
    "SpotifyTrack",
    "SpotifyTrackRecord",
    "SPOTIFY_CLIENT_ID_ENV",
    "TOKEN_PATH",
    "TokenSet",
    "build_authorization_url",
    "build_loopback_redirect_uri",
    "code_challenge_for_verifier",
    "create_pkce_pair",
    "generate_code_verifier",
    "generate_pkce_challenge",
    "generate_pkce_verifier",
    "generate_state",
    "parse_library",
    "parse_playlist",
    "parse_retry_after",
]
