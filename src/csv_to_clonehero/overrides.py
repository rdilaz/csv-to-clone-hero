"""Optional manual overrides loaded from overrides.yaml."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from csv_to_clonehero.normalization import normalize_artist, normalize_title


class ForceAcceptRule(BaseModel):
    track_key: str
    chart_id: int


class ArtistOverride(BaseModel):
    expansion_disabled: bool = False
    expansion_cap: int | None = None
    alias: str = ""


class Overrides(BaseModel):
    force_accept: list[ForceAcceptRule] = Field(default_factory=list)
    reject_charts: list[int] = Field(default_factory=list)
    ignore_tracks: list[str] = Field(default_factory=list)
    artists: dict[str, ArtistOverride] = Field(default_factory=dict)
    title_aliases: dict[str, str] = Field(default_factory=dict)

    def artist_override(self, artist_norm: str) -> ArtistOverride | None:
        for name, override in self.artists.items():
            if normalize_artist(name) == artist_norm:
                return override
        return None

    def artist_alias(self, artist_norm: str) -> str:
        override = self.artist_override(artist_norm)
        return override.alias if override and override.alias else ""

    def title_alias(self, title: str) -> str:
        title_norm = normalize_title(title)
        for original, alias in self.title_aliases.items():
            if normalize_title(original) == title_norm:
                return alias
        return ""


def load_overrides(base_dir: Path) -> Overrides:
    path = base_dir / "overrides.yaml"
    if not path.exists():
        return Overrides()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Overrides.model_validate(raw)
