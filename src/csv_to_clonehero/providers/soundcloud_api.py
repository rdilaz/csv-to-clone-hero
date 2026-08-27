"""Official SoundCloud OAuth and Web API adapter.

Only documented SoundCloud hosts and endpoints are used here.  The adapter
returns neutral provider snapshots and never downloads audio.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx

from csv_to_clonehero.providers.base import (
    LibrarySnapshot,
    ProviderPlaylist,
    ProviderTrack,
    merge_provider_tracks,
)
from csv_to_clonehero.spotify import PKCEPair, code_challenge_for_verifier, generate_state


AUTHORIZE_URL = "https://secure.soundcloud.com/authorize"
TOKEN_URL = "https://secure.soundcloud.com/oauth/token"
API_URL = "https://api.soundcloud.com"
DEFAULT_SCOPES = ("non-expiring",)


class SoundCloudError(RuntimeError):
    """Safe SoundCloud authentication or API failure."""


class RefreshTokenStore(Protocol):
    def get(self, provider: str) -> str | None: ...
    def set(self, provider: str, token: str) -> None: ...
    def delete(self, provider: str) -> None: ...


@dataclass(frozen=True)
class SoundCloudToken:
    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    expires_at: float | None = None
    token_type: str = "Bearer"
    scope: tuple[str, ...] = ()

    def expired(self, now: float | None = None) -> bool:
        return self.expires_at is not None and self.expires_at <= (time.time() if now is None else now) + 30

    @classmethod
    def from_payload(cls, payload: dict[str, Any], previous: "SoundCloudToken | None" = None) -> "SoundCloudToken":
        access = str(payload.get("access_token") or "").strip()
        if not access:
            raise SoundCloudError("SoundCloud did not return an access token")
        raw_expires = payload.get("expires_in")
        expires_at = None
        if raw_expires not in (None, ""):
            try:
                expires_at = time.time() + float(raw_expires)
            except (TypeError, ValueError) as exc:
                raise SoundCloudError("SoundCloud returned an invalid token expiry") from exc
        scopes = payload.get("scope", ())
        if isinstance(scopes, str):
            scopes = tuple(scopes.split())
        return cls(
            access_token=access,
            refresh_token=str(payload.get("refresh_token") or (previous.refresh_token if previous else "")) or None,
            expires_at=expires_at,
            token_type=str(payload.get("token_type") or "Bearer"),
            scope=tuple(str(value) for value in scopes or ()),
        )


@dataclass(frozen=True)
class SoundCloudAuthorizationRequest:
    url: str = field(repr=False)
    state: str = field(repr=False)
    code_verifier: str = field(repr=False)
    redirect_uri: str


class SoundCloudOAuth:
    """Authorization Code + PKCE client for the official SoundCloud API."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        *,
        scopes: tuple[str, ...] = DEFAULT_SCOPES,
        store: RefreshTokenStore | None = None,
        http_client: Any | None = None,
        timeout: float = 20.0,
    ) -> None:
        if not client_id.strip() or not client_secret.strip():
            raise SoundCloudError("SoundCloud credentials are not configured")
        if not redirect_uri.strip():
            raise SoundCloudError("SoundCloud redirect URI is not configured")
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self.redirect_uri = redirect_uri.strip()
        self.scopes = tuple(scopes)
        self.store = store
        self._http = http_client or httpx.Client(timeout=timeout, follow_redirects=True)
        self._owns_http = http_client is None
        self._pending: SoundCloudAuthorizationRequest | None = None
        self._token: SoundCloudToken | None = None

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def begin_authorization(self, *, state: str | None = None, code_verifier: str | None = None) -> SoundCloudAuthorizationRequest:
        verifier = code_verifier or PKCEPair.create().verifier
        actual_state = state or generate_state()
        query = urlencode({
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.scopes),
            "state": actual_state,
            "code_challenge": code_challenge_for_verifier(verifier),
            "code_challenge_method": "S256",
        })
        request = SoundCloudAuthorizationRequest(f"{AUTHORIZE_URL}?{query}", actual_state, verifier, self.redirect_uri)
        self._pending = request
        return request

    def exchange_code(self, code: str, *, returned_state: str, expected_state: str | None, code_verifier: str, redirect_uri: str | None = None) -> SoundCloudToken:
        if not code.strip() or not code_verifier.strip():
            raise SoundCloudError("SoundCloud authorization code and PKCE verifier are required")
        if not expected_state or not secrets.compare_digest(returned_state, expected_state):
            raise SoundCloudError("SoundCloud OAuth state validation failed")
        payload = self._token_request({
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": redirect_uri or self.redirect_uri,
            "code_verifier": code_verifier,
        })
        return self._save(SoundCloudToken.from_payload(payload))

    def refresh(self, refresh_token: str | None = None) -> SoundCloudToken:
        actual = refresh_token or (self._token.refresh_token if self._token else None) or self._stored_token()
        if not actual:
            raise SoundCloudError("SoundCloud sign-in is required")
        payload = self._token_request({
            "grant_type": "refresh_token",
            "refresh_token": actual,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        })
        return self._save(SoundCloudToken.from_payload(payload, self._token or SoundCloudToken("unused", actual)))

    def access_token(self) -> str:
        if self._token is None or self._token.expired():
            self.refresh()
        assert self._token is not None
        return self._token.access_token

    def logout(self) -> None:
        self._token = None
        if self.store:
            deleter = getattr(self.store, "delete", None)
            if callable(deleter):
                try:
                    deleter("soundcloud")
                    return
                except TypeError:
                    pass
            deleter = getattr(self.store, "delete_password", None)
            if callable(deleter):
                deleter("soundcloud", "soundcloud")

    def _save(self, token: SoundCloudToken) -> SoundCloudToken:
        self._token = token
        if token.refresh_token and self.store:
            setter = getattr(self.store, "set", None)
            if callable(setter):
                try:
                    setter("soundcloud", token.refresh_token)
                except TypeError:
                    setter("soundcloud", "soundcloud", token.refresh_token)
            else:
                self.store.set_password("soundcloud", "soundcloud", token.refresh_token)
        return token

    def _stored_token(self) -> str | None:
        if not self.store:
            return None
        getter = getattr(self.store, "get", None)
        if callable(getter):
            try:
                return getter("soundcloud")
            except TypeError:
                return getter("soundcloud", "soundcloud")
        getter = getattr(self.store, "get_password", None)
        return getter("soundcloud", "soundcloud") if callable(getter) else None

    def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        try:
            response = self._http.post(TOKEN_URL, data=data, headers={"Accept": "application/json"})
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise SoundCloudError("SoundCloud token request failed") from exc
        if not isinstance(payload, dict):
            raise SoundCloudError("SoundCloud token response was invalid")
        return payload


class SoundCloudClient:
    """Official account/library API client with linked-partition pagination."""

    def __init__(self, oauth: SoundCloudOAuth, *, http_client: Any | None = None, timeout: float = 20.0) -> None:
        self.oauth = oauth
        self._http = http_client or httpx.Client(timeout=timeout, follow_redirects=True)
        self._owns_http = http_client is None

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def _get(self, path_or_url: str, **params: Any) -> dict[str, Any] | list[Any]:
        url = path_or_url if path_or_url.startswith("http") else f"{API_URL}{path_or_url}"
        response = self._http.get(url, params=params or None, headers={"Authorization": f"OAuth {self.oauth.access_token()}"})
        try:
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SoundCloudError("SoundCloud API request failed") from exc
        if not isinstance(payload, (dict, list)):
            raise SoundCloudError("SoundCloud API returned invalid JSON")
        return payload

    def me(self) -> dict[str, Any]:
        payload = self._get("/me")
        return payload if isinstance(payload, dict) else {}

    def playlists(self) -> list[dict[str, Any]]:
        return [item for item in self._paginate("/me/playlists") if isinstance(item, dict)]

    get_playlists = playlists

    def playlist_tracks(self, playlist_id: str) -> list[dict[str, Any]]:
        return [item for item in self._paginate(f"/playlists/{playlist_id}/tracks") if isinstance(item, dict)]

    def liked_tracks(self) -> list[dict[str, Any]]:
        return [item for item in self._paginate("/users/me/likes/tracks") if isinstance(item, dict)]

    get_liked_tracks = liked_tracks

    def _paginate(self, path: str) -> list[Any]:
        result: list[Any] = []
        next_href: str | None = path
        while next_href:
            payload = self._get(next_href, linked_partitioning="true")
            if isinstance(payload, list):
                result.extend(payload)
                next_href = None
            else:
                result.extend(payload.get("collection") or payload.get("data") or [])
                next_href = str(payload.get("next_href") or "") or None
        return result

    paginate = _paginate


class SoundCloudAPIProvider:
    name = "soundcloud"

    def __init__(self, client: SoundCloudClient):
        self.client = client

    def capabilities(self) -> dict[str, bool]:
        return {"official_api": True, "oauth": True, "user_export": True, "public_playlist": False}

    def import_library(self, *, scope: dict[str, Any] | None = None) -> LibrarySnapshot:
        scope = scope or {}
        selected_ids = {str(value) for value in scope.get("playlist_ids", ())}
        playlists = self.client.playlists()
        playlist_models = [ProviderPlaylist("soundcloud", str(row.get("id") or ""), str(row.get("title") or row.get("name") or "")) for row in playlists if str(row.get("id") or "") in selected_ids]
        tracks: list[ProviderTrack] = []
        for playlist in playlist_models:
            for row in self.client.playlist_tracks(playlist.provider_id):
                track = _api_track(row, playlist)
                if track:
                    tracks.append(track)
        if scope.get("include_liked"):
            for row in self.client.liked_tracks():
                track = _api_track(row, None)
                if track:
                    track.liked = True
                    tracks.append(track)
        return LibrarySnapshot("soundcloud", merge_provider_tracks(tracks), playlist_models, {"import": "api"})

    def close(self) -> None:
        self.client.close()


def _api_track(row: Any, playlist: ProviderPlaylist | None) -> ProviderTrack | None:
    if not isinstance(row, dict):
        return None
    item = row.get("track") if isinstance(row.get("track"), dict) else row
    user = item.get("user") if isinstance(item.get("user"), dict) else {}
    title = str(item.get("title") or "").strip()
    artist = str(user.get("username") or item.get("artist") or "").strip()
    if not title or not artist or not item.get("id"):
        return None
    return ProviderTrack(
        provider="soundcloud",
        provider_id=str(item["id"]),
        title=title,
        artist=artist,
        playlist_ids=[playlist.provider_id] if playlist else [],
        playlist_names=[playlist.name] if playlist else [],
        duration_ms=int(item["duration"]) if str(item.get("duration") or "").isdigit() else None,
        url=str(item.get("permalink_url") or ""),
        metadata={"soundcloud": True},
    )
