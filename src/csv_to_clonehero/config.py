"""Configuration loading.

Reads ``config.yaml`` (created with defaults on first run) using pydantic
models. Relative paths are resolved against the config file's directory.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

import yaml
from pydantic import BaseModel, Field, model_validator


SPOTIFY_CLIENT_ID_ENV = "SPOTIFY_CLIENT_ID"
SPOTIFY_REDIRECT_PORT = 8765
SPOTIFY_REDIRECT_PATH = "/api/auth/spotify/callback"
SPOTIFY_CREDENTIAL_SERVICE = "csv-to-clonehero.spotify"

# Production secret environment variables. These are always read from the
# environment at runtime and never stored in config.yaml (which is gitignored).
SECRET_KEY_ENV = "SECRET_KEY"  # session-cookie signing key
TOKEN_ENCRYPTION_KEY_ENV = "TOKEN_ENCRYPTION_KEY"  # Fernet key for refresh tokens
APP_BASE_URL_ENV = "APP_BASE_URL"  # public HTTPS origin
DATABASE_URL_ENV = "DATABASE_URL"  # optional absolute sqlite path override
NOTIFICATIONS_API_KEY_ENV = "NOTIFICATIONS_API_KEY"  # transactional email key
GOFUNDME_URL_ENV = "GOFUNDME_URL"

# Required in production (when server.enforce_secret_key is true). Keep the
# defaults empty so the existing offline CLI and test flows keep working.
PRODUCTION_SECRET_LABEL = "SECRET_KEY"
PRODUCTION_CRYPTO_LABEL = "TOKEN_ENCRYPTION_KEY"
SPOTIFY_SCOPES = (
    "user-read-private",
    "playlist-read-private",
    "playlist-read-collaborative",
    "user-library-read",
)
DEFAULT_RESULT_CAP = 250
MATCHER_VERSION = "spotify-v1"
DEFAULT_DURATION_TOLERANCE_MS = 5_000

DEFAULT_COMMON_SONGS_PATHS = (
    "~/Documents/My Games/Clone Hero/Songs",
    "~/Documents/My Games/YARG/Songs",
    "~/AppData/Roaming/YARG/Songs",
    "~/AppData/Local/YARG/Songs",
    "Songs",
)


class PathsConfig(BaseModel):
    input_dir: str = "input_playlists"
    output_dir: str = "output_library"
    clone_hero_songs_path: str = ""
    reports_dir: str = "reports"
    state_dir: str = "state"
    # Base-dir-relative on purpose: state must never live inside the output
    # folder, so per-run output overrides never carry the DB with them.
    database: str = "state/csv_to_clonehero.db"
    catalog: str = "catalog/enchor.jsonl"
    artifact_cache_dir: str = "state/export-artifacts"
    manifest_dir: str = "state/export-manifests"


class BuildConfig(BaseModel):
    dry_run: bool = False
    direct_download_enabled: bool = True


class MatchingConfig(BaseModel):
    auto_accept_threshold: float = 90.0
    review_threshold: float = 70.0
    download_review_matches: bool = False
    artist_conflict_threshold: float = 55.0
    # Spotify-workflow names. The legacy threshold fields remain the source of
    # truth for existing callers and are kept synchronized at model creation.
    recommendation_threshold: float = 90.0
    export_floor: float = 70.0
    duration_tolerance_ms: int = DEFAULT_DURATION_TOLERANCE_MS
    matcher_version: str = MATCHER_VERSION

    def model_post_init(self, __context: object) -> None:
        """Keep new threshold names compatible with the legacy pipeline."""
        fields_set = self.model_fields_set
        if "recommendation_threshold" in fields_set and "auto_accept_threshold" not in fields_set:
            self.auto_accept_threshold = self.recommendation_threshold
        elif "auto_accept_threshold" in fields_set and "recommendation_threshold" not in fields_set:
            self.recommendation_threshold = self.auto_accept_threshold
        if "export_floor" in fields_set and "review_threshold" not in fields_set:
            self.review_threshold = self.export_floor
        elif "review_threshold" in fields_set and "export_floor" not in fields_set:
            self.export_floor = self.review_threshold

    @property
    def recommendation_score_threshold(self) -> float:
        return self.recommendation_threshold

    @property
    def export_eligibility_floor(self) -> float:
        return self.export_floor

    @property
    def duration_tolerance_seconds(self) -> float:
        return self.duration_tolerance_ms / 1000.0


class InstrumentsConfig(BaseModel):
    require_five_fret_guitar: bool = True
    allow_six_fret_only: bool = False
    allow_no_guitar: bool = False
    exclude_modcharts: bool = True


class ExpansionConfig(BaseModel):
    enabled: bool = True
    max_artists_per_batch: int = Field(default=25, ge=1, le=25)
    cap_many_songs: int = 50
    min_songs_many: int = 5
    cap_some_songs: int = 25
    cap_one_song: int = 10
    cap_featured_only: int = 0
    min_score: float = 80.0


class StorageConfig(BaseModel):
    max_library_gib: float = 48.0
    estimated_chart_bytes: int = 30 * 1024 * 1024

    @property
    def max_library_bytes(self) -> int:
        return int(self.max_library_gib * (1024**3))


class NetworkConfig(BaseModel):
    request_delay_seconds: float = 0.5
    concurrency: int = 2
    retries: int = 3
    timeout_seconds: float = 30.0
    cache_ttl_days: int = 7
    # Skip per-chart HEAD size probes during export preflight (Enchor's file
    # CDN tolerates downloads without them; the size only feeds an estimate).
    head_size_enabled: bool = False
    # Light delay between download starts (seconds). The file CDN is separate
    # from the search API and is not governed by request_delay_seconds.
    download_request_delay: float = 0.1


class CacheConfig(BaseModel):
    """Controls persistent discovery/search cache reuse."""

    enabled: bool = True
    ttl_days: int = 7
    cache_errors: bool = False
    resume: bool = True

    @property
    def cache_ttl_days(self) -> int:
        return self.ttl_days


class ScanConfig(BaseModel):
    """Controls the blocking, resumable Spotify/source discovery pass."""

    blocking: bool = True
    resume: bool = True
    persist_progress: bool = True
    retry_failed_queries: bool = True
    status_poll_interval_ms: int = 1200


class SearchConfig(BaseModel):
    # Catalog-first exact pass: artists with enough unique songs are searched
    # once by artist; their tracks are scored locally against the catalog.
    artist_catalog_first: bool = True
    artist_catalog_min_songs: int = 3
    # Maximum normal search results retained per source/logical query.
    result_cap: int = DEFAULT_RESULT_CAP
    max_search_pages: int = 20
    # Full source responses stay cached, but only the strongest local matches
    # receive expensive scoring and workspace rows.
    candidate_shortlist_size: int = 50
    # Local complete-catalog retrieval settings. These affect candidate
    # generation only; score_candidate() remains the acceptance authority.
    catalog_artist_fuzzy_limit: int = 60
    catalog_title_fuzzy_limit: int = 50
    catalog_min_artist_similarity: float = 55.0
    catalog_min_title_similarity: float = 20.0

    @property
    def search_result_cap(self) -> int:
        return self.result_cap

    @property
    def max_results(self) -> int:
        return self.result_cap


class SourcesConfig(BaseModel):
    enabled: list[str] = Field(default_factory=lambda: ["enchor"])


class SoundCloudConfig(BaseModel):
    """Optional approved SoundCloud integration coordinates.

    The public product may use an official OAuth client when configured. User
    exports remain available without storing SoundCloud credentials.
    """

    enabled: bool = False
    client_id_env: str = "SOUNDCLOUD_CLIENT_ID"
    client_secret_env: str = "SOUNDCLOUD_CLIENT_SECRET"
    redirect_uri: str = ""
    redirect_path: str = "/api/auth/soundcloud/callback"
    allow_user_exports: bool = True
    allow_public_playlists: bool = True
    scopes: list[str] = Field(default_factory=lambda: ["non-expiring"])

    @property
    def effective_client_id(self) -> str:
        return os.environ.get(self.client_id_env.strip() or "SOUNDCLOUD_CLIENT_ID", "").strip()

    @property
    def effective_client_secret(self) -> str:
        return os.environ.get(self.client_secret_env.strip() or "SOUNDCLOUD_CLIENT_SECRET", "").strip()

    @property
    def oauth_configured(self) -> bool:
        return bool(self.effective_client_id and self.effective_client_secret and self.redirect_uri_value)

    @property
    def redirect_uri_value(self) -> str:
        configured = self.redirect_uri.strip()
        if configured:
            return configured
        return f"http://127.0.0.1:8765{self.redirect_path}"


class KeyringConfig(BaseModel):
    """Non-secret keyring coordinates for the Spotify refresh token."""

    service: str = SPOTIFY_CREDENTIAL_SERVICE
    user: str = ""
    username: str = ""

    @property
    def effective_username(self) -> str:
        return (self.username or self.user).strip()

    @property
    def configured(self) -> bool:
        return bool(self.service.strip() and self.effective_username)

    @property
    def backend_available(self) -> bool:
        """Return whether the optional keyring package can be imported."""
        try:
            import keyring  # type: ignore[import-not-found]

            return callable(getattr(keyring, "get_password", None)) and callable(
                getattr(keyring, "set_password", None)
            )
        except Exception:
            return False


class SpotifyConfig(BaseModel):
    """Spotify public client and PKCE/keyring settings.

    Spotify client IDs are public application identifiers. No client secret is
    represented here; the private-beta flow uses Authorization Code + PKCE.
    """

    client_id: str = Field(default_factory=lambda: os.environ.get(SPOTIFY_CLIENT_ID_ENV, ""))
    client_id_env: str = SPOTIFY_CLIENT_ID_ENV
    redirect_port: int = SPOTIFY_REDIRECT_PORT
    redirect_path: str = SPOTIFY_REDIRECT_PATH
    # HTTPS origin of a reverse proxy or tunnel for remote browser access.
    # Its callback must be registered with the Spotify application.
    public_base_url: str = ""
    scopes: list[str] = Field(default_factory=lambda: list(SPOTIFY_SCOPES))
    keyring: KeyringConfig = Field(default_factory=KeyringConfig)

    @model_validator(mode="before")
    @classmethod
    def _accept_flat_keyring_settings(cls, value: object) -> object:
        """Accept flat keyring names while keeping ``spotify.keyring`` clear."""
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        raw_keyring = data.get("keyring")
        if isinstance(raw_keyring, KeyringConfig):
            keyring = raw_keyring.model_dump()
        elif isinstance(raw_keyring, Mapping):
            keyring = dict(raw_keyring)
        else:
            keyring = {}
        for source, target in (
            ("keyring_service", "service"),
            ("keyring_user", "user"),
            ("keyring_username", "username"),
            ("credential_service", "service"),
            ("credential_username", "username"),
        ):
            if source in data and target not in keyring:
                keyring[target] = data[source]
        if keyring:
            data["keyring"] = keyring
        if isinstance(data.get("scopes"), str):
            data["scopes"] = data["scopes"].split()
        return data

    def model_post_init(self, __context: object) -> None:
        """Materialize an environment fallback without overriding YAML values."""
        configured = str(self.client_id or "").strip()
        if not configured or configured in {
            f"${{{self.client_id_env}}}",
            f"%{self.client_id_env}%",
        }:
            variable = self.client_id_env.strip() or SPOTIFY_CLIENT_ID_ENV
            self.client_id = os.environ.get(variable, "").strip()

    @property
    def effective_client_id(self) -> str:
        """Return the configured ID, falling back to the current environment."""
        configured = str(self.client_id or "").strip()
        if configured and configured not in {
            f"${{{self.client_id_env}}}",
            f"%{self.client_id_env}%",
        }:
            return configured
        variable = self.client_id_env.strip() or SPOTIFY_CLIENT_ID_ENV
        return os.environ.get(variable, "").strip()

    @property
    def client_id_configured(self) -> bool:
        return bool(self.effective_client_id)

    @property
    def normalized_scopes(self) -> tuple[str, ...]:
        values: list[str]
        if isinstance(self.scopes, str):
            values = self.scopes.split()
        else:
            values = [str(scope).strip() for scope in self.scopes]
        return tuple(dict.fromkeys(scope for scope in values if scope))

    @property
    def redirect_uri(self) -> str:
        """Return a safe loopback or configured HTTPS callback URI."""
        public_base_url = self.public_base_url.strip().rstrip("/")
        if public_base_url:
            parsed = urlsplit(public_base_url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or not self.redirect_path.startswith("/")
                or "?" in self.redirect_path
                or "#" in self.redirect_path
            ):
                return ""
            return urlunsplit(("https", parsed.netloc, parsed.path.rstrip("/") + self.redirect_path, "", ""))
        if (
            not isinstance(self.redirect_port, int)
            or isinstance(self.redirect_port, bool)
            or not 1 <= self.redirect_port <= 65535
        ):
            return ""
        if (
            not isinstance(self.redirect_path, str)
            or not self.redirect_path.startswith("/")
            or "?" in self.redirect_path
            or "#" in self.redirect_path
        ):
            return ""
        return f"http://127.0.0.1:{self.redirect_port}{self.redirect_path}"

    @property
    def keyring_service(self) -> str:
        return self.keyring.service.strip()

    @keyring_service.setter
    def keyring_service(self, value: str) -> None:
        self.keyring.service = value

    @property
    def keyring_user(self) -> str:
        return self.keyring.effective_username

    @keyring_user.setter
    def keyring_user(self, value: str) -> None:
        self.keyring.user = value
        self.keyring.username = value

    @property
    def keyring_username(self) -> str:
        return self.keyring_user

    @keyring_username.setter
    def keyring_username(self, value: str) -> None:
        self.keyring_user = value

    @property
    def credential_service(self) -> str:
        return self.keyring_service

    @property
    def credential_username(self) -> str:
        # The client ID keeps credentials isolated if a different shared beta
        # application is configured, while an explicit user setting wins.
        return self.keyring_user or self.effective_client_id or "default"

    @property
    def keyring_configuration_ready(self) -> bool:
        return bool(self.keyring_service and self.credential_username)

    @property
    def keyring_ready(self) -> bool:
        """Return config readiness without contacting or reading the keyring."""
        return self.keyring_configuration_ready

    @property
    def configuration_ready(self) -> bool:
        return bool(self.effective_client_id and self.redirect_uri and self.normalized_scopes)

    @property
    def oauth_configuration_ready(self) -> bool:
        return self.configuration_ready


class ExportConfig(BaseModel):
    """Safety defaults for direct-folder and ZIP exports."""

    managed_sync: bool = False
    overwrite_owned: bool = True
    include_manifest_in_zip: bool = True
    minimum_free_bytes: int = 0
    # Number of concurrent download workers; 1 is serial. Enchor's file CDN
    # tolerates a handful of parallel streams, which hides per-request latency.
    download_workers: int = 3
    continue_on_error: bool = True
    artifact_retries: int = 2
    retry_backoff_seconds: float = 2.0


class SongsFolderDetectionConfig(BaseModel):
    """Safe, non-destructive hints for locating common Songs folders."""

    enabled: bool = True
    include_configured_output: bool = True
    include_portable: bool = True
    common_paths: list[str] = Field(default_factory=lambda: list(DEFAULT_COMMON_SONGS_PATHS))
    max_candidates: int = 32

    @property
    def locations(self) -> list[str]:
        return self.common_paths


class InventoryConfig(BaseModel):
    """Bounded, read-only local Songs-folder inventory settings."""

    max_files: int = 100_000
    max_depth: int = 64
    max_file_bytes: int = 2 * 1024 * 1024 * 1024
    max_total_bytes: int = 0
    max_metadata_bytes: int = 1 * 1024 * 1024
    max_members: int = 4096
    max_member_bytes: int = 512 * 1024 * 1024
    max_archive_bytes: int = 2 * 1024 * 1024 * 1024
    max_compression_ratio: float = 10_000.0
    hash_files: bool = False
    poll_interval_ms: int = 1200


class HierarchyConfig(BaseModel):
    root_folder: str = "00 - His Music"
    exact_section: str = "Exact Playlist Songs"
    expansion_section: str = "Artist Expansion"


class LibraryConfig(BaseModel):
    # Folder layout under the library root:
    #   hybrid       - exact: <Artist>/<Album|Singles>/, expansion: <Pack|Artist>/
    #   artist_album - everything under <Artist>/<Album|Singles>/
    #   pack         - everything under <Pack>/ (fallback <Artist>/)
    #   legacy       - <root_folder>/<Exact Playlist Songs|Artist Expansion>/<Artist>/
    # The hierarchy.* keys above only apply to the legacy layout.
    layout: Literal["hybrid", "artist_album", "pack", "legacy"] = "hybrid"


class LoggingConfig(BaseModel):
    level: str = "INFO"


class CryptoConfig(BaseModel):
    """Encryption of per-user refresh tokens. Key always comes from env."""

    key_env: str = TOKEN_ENCRYPTION_KEY_ENV

    @property
    def effective_key_env(self) -> str:
        return (self.key_env or TOKEN_ENCRYPTION_KEY_ENV).strip()

    def load_key(self) -> str | None:
        value = os.environ.get(self.effective_key_env, "").strip()
        return value or None


class SessionsConfig(BaseModel):
    """Server-side session lifecycle for the multi-user web app."""

    cookie_name: str = "c2ch_session"
    cookie_same_site: str = "Lax"
    cookie_secure: bool | None = None  # None -> enabled in production
    max_age_seconds: int = 30 * 24 * 3600
    domain: str = ""


class ServerConfig(BaseModel):
    """Public cloud deployment settings for the multi-user web app.

    ``base_url`` is the HTTPS origin users reach (charts.ryo-nd.com). It
    doubles as the Spotify OAuth redirect origin, so it must be registered in
    the Spotify Developer Dashboard.
    """

    base_url_env: str = APP_BASE_URL_ENV
    base_url: str = ""
    secret_key_env: str = SECRET_KEY_ENV
    enforce_secret_key: bool = False
    trusted_hops: int = 1
    database_url_env: str = DATABASE_URL_ENV
    database_url: str = ""
    max_content_length: int = 50 * 1024 * 1024
    enchor_preflight_ttl_seconds: int = 3600
    max_scope_playlists: int = 200
    max_scope_tracks: int = 10000
    enable_expansion: bool = False
    session: SessionsConfig = Field(default_factory=SessionsConfig)

    @property
    def effective_base_url(self) -> str:
        env_value = os.environ.get(self.base_url_env, "").strip()
        return env_value or self.base_url.strip().rstrip("/")

    @property
    def effective_database_url(self) -> str:
        env_value = os.environ.get(self.database_url_env, "").strip()
        return env_value or self.database_url.strip()

    def require_secrets(self) -> None:
        """Raise a clear error when production secrets are missing.

        Only runs when the server is actually started in production mode so
        the offline CLI and test suite stay unaffected.
        """
        if not self.effective_base_url:
            raise RuntimeError(
                f"APP_BASE_URL is not set; configure server.base_url or {self.base_url_env}"
            )
        if not self.enforce_secret_key:
            return
        if not os.environ.get(self.secret_key_env, "").strip():
            raise RuntimeError(
                f"{self.secret_key_env} is required for the multi-user server; "
                "generate one with `python -m csv_to_clonehero keys` "
                "and set it in the environment/secret manager"
            )


class NotificationsConfig(BaseModel):
    """Transactional email for long-job completion (not accounts)."""

    enabled: bool = True
    provider: str = "resend"  # resend | smtp | log
    api_key_env: str = NOTIFICATIONS_API_KEY_ENV
    from_address: str = "noreply@ryo-nd.com"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user_env: str = ""
    smtp_password_env: str = ""
    smtp_use_tls: bool = True

    @property
    def effective_api_key(self) -> str:
        return os.environ.get(self.api_key_env, "").strip()


class MonetizationConfig(BaseModel):
    """Site monetization: tasteful ad slots and the creator GoFundMe."""

    ads_enabled: bool = True
    ad_banner_slot: str = ""
    ad_sidebar_slot: str = ""
    gofundme_enabled: bool = True
    gofundme_url_env: str = GOFUNDME_URL_ENV
    gofundme_url: str = ""
    gofundme_banner: bool = True

    @property
    def effective_gofundme_url(self) -> str:
        env_value = os.environ.get(self.gofundme_url_env, "").strip()
        return env_value or self.gofundme_url.strip()


class AdminConfig(BaseModel):
    """Optional legacy surfaces that should not face the public product."""

    enable_legacy_csv: bool = False
    admins: list[str] = Field(default_factory=list)  # Spotify user ids


class AppConfig(BaseModel):
    paths: PathsConfig = Field(default_factory=PathsConfig)
    build: BuildConfig = Field(default_factory=BuildConfig)
    matching: MatchingConfig = Field(default_factory=MatchingConfig)
    instruments: InstrumentsConfig = Field(default_factory=InstrumentsConfig)
    expansion: ExpansionConfig = Field(default_factory=ExpansionConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    soundcloud: SoundCloudConfig = Field(default_factory=SoundCloudConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    scan: ScanConfig = Field(default_factory=ScanConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    spotify: SpotifyConfig = Field(default_factory=SpotifyConfig)
    export: ExportConfig = Field(default_factory=ExportConfig)
    songs_folder_detection: SongsFolderDetectionConfig = Field(
        default_factory=SongsFolderDetectionConfig
    )
    inventory: InventoryConfig = Field(default_factory=InventoryConfig)
    hierarchy: HierarchyConfig = Field(default_factory=HierarchyConfig)
    library: LibraryConfig = Field(default_factory=LibraryConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    crypto: CryptoConfig = Field(default_factory=CryptoConfig)
    sessions: SessionsConfig = Field(default_factory=SessionsConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    monetization: MonetizationConfig = Field(default_factory=MonetizationConfig)
    admin: AdminConfig = Field(default_factory=AdminConfig)

    # Resolved lazily by load_config()
    base_dir: Path = Field(default_factory=Path.cwd, exclude=True)

    # ----- resolved path helpers -------------------------------------------------
    def resolve(self, value: str) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.base_dir / path
        return path

    def _resolve_expanded(self, value: str) -> Path:
        """Resolve a user path after expanding the platform environment."""
        return self.resolve(os.path.expandvars(str(value)))

    @property
    def input_path(self) -> Path:
        return self.resolve(self.paths.input_dir)

    @property
    def reports_path(self) -> Path:
        return self.resolve(self.paths.reports_dir)

    @property
    def database_path(self) -> Path:
        url = self.server.effective_database_url
        if url:
            if url.startswith("sqlite:///"):
                url = url[len("sqlite:///"):]
            elif url.startswith("sqlite://"):
                url = url[len("sqlite://"):].lstrip(":")
            elif url.lower().startswith("postgres"):
                raise RuntimeError(
                    "Postgres DATABASE_URL is not supported yet; this deployment "
                    "is single-instance with a SQLite volume (see runbook.md). "
                    f"Got: {url.split('@')[-1]}"
                )
            if url:
                return self.resolve(url)
        return self.resolve(self.paths.database)

    @property
    def catalog_path(self) -> Path:
        """Location of the optional generated Enchor metadata snapshot."""
        return self._resolve_expanded(self.paths.catalog)

    @property
    def public_base_url(self) -> str:
        return self.server.effective_base_url

    def require_secrets_for_production(self) -> None:
        self.server.require_secrets()

    @property
    def state_path(self) -> Path:
        """Directory for persistent state, always outside the output root."""
        return self.database_path.parent

    @property
    def artifact_cache_path(self) -> Path:
        configured = str(self.paths.artifact_cache_dir or "").strip()
        return self._resolve_expanded(configured) if configured else self.state_path / "export-artifacts"

    @property
    def manifest_state_path(self) -> Path:
        configured = str(self.paths.manifest_dir or "").strip()
        return self._resolve_expanded(configured) if configured else self.state_path / "export-manifests"

    @property
    def manifest_dir(self) -> Path:
        return self.manifest_state_path

    @property
    def artifact_cache_dir(self) -> Path:
        return self.artifact_cache_path

    @property
    def manifest_state_dir(self) -> Path:
        return self.manifest_state_path

    # ----- Spotify readiness helpers -------------------------------------------
    @property
    def spotify_client_id(self) -> str:
        return self.spotify.effective_client_id

    @property
    def spotify_configuration_ready(self) -> bool:
        return self.spotify.configuration_ready

    @property
    def spotify_ready(self) -> bool:
        return self.spotify_configuration_ready and self.keyring_configuration_ready

    @property
    def spotify_redirect_uri(self) -> str:
        """Build the OAuth redirect URI.

        The multi-user (server) product uses the public base URL as the OAuth
        origin; the single-user local app keeps its loopback redirect unless an
        explicit HTTPS ``spotify.public_base_url`` is configured.
        """
        if self.spotify.public_base_url.strip():
            return self.spotify.redirect_uri
        public = self.server.effective_base_url
        if public and self.spotify.redirect_path.startswith("/"):
            from urllib.parse import urlunsplit, urlsplit

            parsed = urlsplit(public)
            if parsed.scheme == "https" and parsed.hostname:
                return urlunsplit(
                    (
                        parsed.scheme,
                        parsed.netloc,
                        parsed.path.rstrip("/") + self.spotify.redirect_path,
                        "",
                        "",
                    )
                )
        return self.spotify.redirect_uri

    @property
    def spotify_scopes(self) -> tuple[str, ...]:
        return self.spotify.normalized_scopes

    @property
    def spotify_configuration_errors(self) -> tuple[str, ...]:
        errors: list[str] = []
        if not self.spotify_client_id:
            errors.append(
                f"Spotify client ID is not configured; set spotify.client_id or {SPOTIFY_CLIENT_ID_ENV}"
            )
        if not self.spotify.redirect_uri:
            errors.append("Spotify OAuth redirect port/path is invalid")
        if not self.spotify.normalized_scopes:
            errors.append("Spotify OAuth scopes are empty")
        return tuple(errors)

    @property
    def spotify_configuration_error(self) -> str:
        return self.spotify_configuration_errors[0] if self.spotify_configuration_errors else ""

    @property
    def keyring_service(self) -> str:
        return self.spotify.keyring_service

    @property
    def keyring_user(self) -> str:
        return self.spotify.credential_username

    @property
    def keyring_configuration_ready(self) -> bool:
        return self.spotify.keyring_configuration_ready

    @property
    def keyring_ready(self) -> bool:
        """Return safe configuration readiness without reading credentials."""
        return self.keyring_configuration_ready

    @property
    def keyring_available(self) -> bool:
        return self.spotify.keyring.backend_available

    @property
    def spotify_oauth_settings(self) -> dict[str, object]:
        """Return non-secret arguments suitable for constructing an OAuth client."""
        return {
            "client_id": self.spotify_client_id,
            "scopes": self.spotify_scopes,
            "redirect_port": self.spotify.redirect_port,
            "redirect_path": self.spotify.redirect_path,
            "redirect_uri": self.spotify_redirect_uri,
            "credential_service": self.spotify.keyring_service,
            "credential_username": self.spotify.credential_username,
        }

    def spotify_oauth_kwargs(self) -> dict[str, object]:
        return dict(self.spotify_oauth_settings)

    # ----- matching/search aliases ---------------------------------------------
    @property
    def result_cap(self) -> int:
        return self.search.result_cap

    @property
    def matcher_version(self) -> str:
        return self.matching.matcher_version

    @property
    def recommendation_threshold(self) -> float:
        return self.matching.recommendation_threshold

    @property
    def export_floor(self) -> float:
        return self.matching.export_floor

    @property
    def duration_tolerance_ms(self) -> int:
        return self.matching.duration_tolerance_ms

    @property
    def cache_ttl_days(self) -> int:
        return self.cache.ttl_days

    # ----- Songs-folder discovery helpers --------------------------------------
    @property
    def common_songs_paths(self) -> list[Path]:
        return self.songs_folder_candidates()

    def songs_folder_candidates(self) -> list[Path]:
        """Return configured/common candidates without creating or touching them."""
        settings = self.songs_folder_detection
        if not settings.enabled:
            return []

        raw_paths: list[str] = []
        if settings.include_configured_output:
            if self.paths.clone_hero_songs_path.strip():
                raw_paths.append(self.paths.clone_hero_songs_path)
            if self.paths.output_dir.strip():
                raw_paths.append(self.paths.output_dir)
        for raw in settings.common_paths:
            value = str(raw).strip()
            if not value:
                continue
            if not settings.include_portable and value in {"Songs", ".\\Songs", "./Songs"}:
                continue
            raw_paths.append(value)

        candidates: list[Path] = []
        seen: set[str] = set()
        try:
            limit = max(0, int(settings.max_candidates))
        except (TypeError, ValueError):
            limit = 32
        for raw in raw_paths:
            try:
                candidate = self._resolve_expanded(raw)
            except (OSError, TypeError, ValueError):
                continue
            key = os.path.normcase(str(candidate))
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)
            if limit and len(candidates) >= limit:
                break
        return candidates

    def detect_songs_folders(self) -> list[Path]:
        """Return existing candidates; this helper never creates directories."""
        found: list[Path] = []
        for candidate in self.songs_folder_candidates():
            try:
                if candidate.is_dir():
                    found.append(candidate)
            except OSError:
                continue
        return found

    find_common_songs_folders = detect_songs_folders

    @property
    def library_root(self) -> Path:
        """Root folder the library hierarchy is built under."""
        if self.paths.clone_hero_songs_path:
            return self.resolve(self.paths.clone_hero_songs_path)
        return self.resolve(self.paths.output_dir)

    def with_output_dir(self, output_dir: str) -> "AppConfig":
        """Per-run override of the output folder only.

        The state DB and reports dir stay pinned to the base dir, so the
        search cache, manual decisions and reports survive any output-folder
        choice (and reports never land inside a user-chosen songs folder).
        """
        return self.model_copy(
            update={
                "paths": self.paths.model_copy(update={"output_dir": output_dir})
            }
        )


DEFAULT_CONFIG_RESOURCE = Path(__file__).resolve().parent / "default_config.yaml"

# Locations earlier versions kept the state DB (base-dir-relative).
LEGACY_DATABASE_LOCATIONS = (
    "output_library/state/library.db",
    "output_library/state/csv_to_clonehero.db",
    "state/library.db",
)


def migrate_state_db(config: AppConfig) -> str:
    """One-time move of a legacy state DB to the configured location.

    Runs at startup, before any connection is opened. Returns a human-readable
    note when a move happened, "" otherwise. Raises RuntimeError when the
    state is ambiguous (both a legacy and the configured DB exist, or several
    legacy DBs) - legacy databases are never silently merged.

    Only applies to the default database path; custom paths are user-managed.
    """
    target = config.database_path
    if target != config.resolve(PathsConfig().database):
        return ""
    legacy = [
        existing
        for rel in LEGACY_DATABASE_LOCATIONS
        if (existing := config.resolve(rel)).exists()
    ]
    if target.exists():
        if legacy:
            raise RuntimeError(
                "Both the state database and a legacy one exist:\n"
                f"  current: {target}\n"
                + "".join(f"  legacy:  {path}\n" for path in legacy)
                + "Delete the legacy file(s) after confirming the current one "
                "is intact, then start again."
            )
        return ""
    if not legacy:
        return ""
    if len(legacy) > 1:
        raise RuntimeError(
            "Multiple legacy state databases exist; refusing to pick one:\n"
            + "".join(f"  {path}\n" for path in legacy)
            + f"Keep the one to continue with, move it to {target}, and delete "
            "the other(s)."
        )
    source = legacy[0]
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    return f"Moved state database from {source} to {target}"


def load_config(config_path: str | Path | None = None) -> AppConfig:
    """Load config.yaml, creating it from the bundled default if missing."""
    if config_path is None:
        config_path = Path.cwd() / "config.yaml"
    config_path = Path(config_path)

    if not config_path.exists():
        # Fall back to the config next to the current working directory's parent
        # (handles being launched from elsewhere) or create a fresh default.
        alt = Path(__file__).resolve().parents[2] / "config.yaml"
        if alt.exists():
            config_path = alt
        else:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            if DEFAULT_CONFIG_RESOURCE.exists():
                shutil.copy(DEFAULT_CONFIG_RESOURCE, config_path)
            else:
                config_path.write_text("# CSV to Clone Hero configuration\n", encoding="utf-8")

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    config = AppConfig.model_validate(raw)
    config.base_dir = config_path.resolve().parent
    return config
