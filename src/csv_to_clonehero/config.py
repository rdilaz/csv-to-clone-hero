"""Configuration loading.

Reads ``config.yaml`` (created with defaults on first run) using pydantic
models. Relative paths are resolved against the config file's directory.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class PathsConfig(BaseModel):
    input_dir: str = "input_playlists"
    output_dir: str = "output_library"
    clone_hero_songs_path: str = ""
    reports_dir: str = "reports"
    database: str = "state/library.db"


class BuildConfig(BaseModel):
    dry_run: bool = False
    direct_download_enabled: bool = True


class MatchingConfig(BaseModel):
    auto_accept_threshold: float = 90.0
    review_threshold: float = 70.0
    download_review_matches: bool = False
    artist_conflict_threshold: float = 55.0


class InstrumentsConfig(BaseModel):
    require_five_fret_guitar: bool = True
    allow_six_fret_only: bool = False
    allow_no_guitar: bool = False
    exclude_modcharts: bool = True


class ExpansionConfig(BaseModel):
    enabled: bool = True
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
    request_delay_seconds: float = 0.25
    retries: int = 3
    timeout_seconds: float = 30.0
    cache_ttl_days: int = 7


class SourcesConfig(BaseModel):
    enabled: list[str] = Field(default_factory=lambda: ["enchor"])


class HierarchyConfig(BaseModel):
    root_folder: str = "00 - His Music"
    exact_section: str = "Exact Playlist Songs"
    expansion_section: str = "Artist Expansion"


class LoggingConfig(BaseModel):
    level: str = "INFO"


class AppConfig(BaseModel):
    paths: PathsConfig = Field(default_factory=PathsConfig)
    build: BuildConfig = Field(default_factory=BuildConfig)
    matching: MatchingConfig = Field(default_factory=MatchingConfig)
    instruments: InstrumentsConfig = Field(default_factory=InstrumentsConfig)
    expansion: ExpansionConfig = Field(default_factory=ExpansionConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    hierarchy: HierarchyConfig = Field(default_factory=HierarchyConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    # Resolved lazily by load_config()
    base_dir: Path = Field(default_factory=Path.cwd, exclude=True)

    # ----- resolved path helpers -------------------------------------------------
    def resolve(self, value: str) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.base_dir / path
        return path

    @property
    def input_path(self) -> Path:
        return self.resolve(self.paths.input_dir)

    @property
    def reports_path(self) -> Path:
        return self.resolve(self.paths.reports_dir)

    @property
    def database_path(self) -> Path:
        return self.resolve(self.paths.database)

    @property
    def library_root(self) -> Path:
        """Root folder the library hierarchy is built under."""
        if self.paths.clone_hero_songs_path:
            return self.resolve(self.paths.clone_hero_songs_path)
        return self.resolve(self.paths.output_dir)


DEFAULT_CONFIG_RESOURCE = Path(__file__).resolve().parent / "default_config.yaml"


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
