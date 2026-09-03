"""SQLite persistent state: tracks, cached searches, candidates, scores,
selections and downloads. Enables caching, resume and idempotent rebuilds.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, fields, is_dataclass
from pathlib import Path
from typing import Any

from csv_to_clonehero.models import ChartCandidate, Track

SCHEMA_VERSION = 19

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS playlist_files (
    id INTEGER PRIMARY KEY,
    filename TEXT NOT NULL UNIQUE,
    row_count INTEGER NOT NULL DEFAULT 0,
    imported_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS spotify_tracks (
    id INTEGER PRIMARY KEY,
    track_key TEXT NOT NULL UNIQUE,
    spotify_id TEXT,
    title TEXT NOT NULL,
    title_norm TEXT NOT NULL,
    artist_full TEXT NOT NULL,
    artist_norm TEXT NOT NULL,
    primary_artist TEXT NOT NULL,
    primary_artist_norm TEXT NOT NULL,
    featured_json TEXT NOT NULL DEFAULT '[]',
    album TEXT,
    album_norm TEXT,
    ignored INTEGER NOT NULL DEFAULT 0,
    album_id TEXT,
    duration_ms INTEGER,
    isrc TEXT,
    spotify_url TEXT,
    origin TEXT,
    origin_json TEXT NOT NULL DEFAULT '{}',
    origin_metadata_json TEXT NOT NULL DEFAULT '{}',
    added_at TEXT,
    spotify_synced_at REAL
);

CREATE TABLE IF NOT EXISTS track_playlists (
    track_id INTEGER NOT NULL REFERENCES spotify_tracks(id),
    playlist_file_id INTEGER NOT NULL REFERENCES playlist_files(id),
    UNIQUE (track_id, playlist_file_id)
);

CREATE TABLE IF NOT EXISTS search_queries (
    id INTEGER PRIMARY KEY,
    query_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    query_text TEXT NOT NULL,
    searched_at REAL NOT NULL,
    result_count INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    source_name TEXT,
    source_version TEXT,
    result_cap INTEGER,
    source_total INTEGER,
    cap_reached INTEGER NOT NULL DEFAULT 0,
    returned_count INTEGER NOT NULL DEFAULT 0,
    complete INTEGER NOT NULL DEFAULT 1,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS search_query_candidates (
    query_id INTEGER NOT NULL REFERENCES search_queries(id),
    candidate_id INTEGER NOT NULL REFERENCES candidate_charts(id),
    UNIQUE (query_id, candidate_id)
);

CREATE TABLE IF NOT EXISTS candidate_charts (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    chart_id INTEGER NOT NULL,
    md5 TEXT,
    data_json TEXT NOT NULL,
    source_artwork_url TEXT,
    source_artwork_id TEXT,
    issue_details_json TEXT NOT NULL DEFAULT '[]',
    issue_severity TEXT,
    review_status TEXT,
    review_url TEXT,
    song_id TEXT,
    version_id TEXT,
    version_group_id TEXT,
    group_id TEXT,
    source_total INTEGER,
    cap_reached INTEGER NOT NULL DEFAULT 0,
    returned_count INTEGER,
    source_metadata_json TEXT NOT NULL DEFAULT '{}',
    diff_band INTEGER,
    diff_guitar_coop INTEGER,
    diff_rhythm INTEGER,
    diff_drums_real INTEGER,
    diff_guitar_coop_ghl INTEGER,
    diff_rhythm_ghl INTEGER,
    diff_bassghl INTEGER,
    diff_guitar_ghl INTEGER,
    diff_bass_ghl INTEGER,
    diff_vocals_harm1 INTEGER,
    diff_vocals_harm2 INTEGER,
    diff_vocals_harm3 INTEGER,
    difficulty_fields_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (source, chart_id)
);

CREATE TABLE IF NOT EXISTS match_scores (
    id INTEGER PRIMARY KEY,
    track_id INTEGER NOT NULL REFERENCES spotify_tracks(id),
    candidate_id INTEGER NOT NULL REFERENCES candidate_charts(id),
    score REAL NOT NULL,
    decision TEXT NOT NULL,
    breakdown_json TEXT NOT NULL,
    UNIQUE (track_id, candidate_id)
);

CREATE TABLE IF NOT EXISTS expansion_artists (
    id INTEGER PRIMARY KEY,
    artist_name TEXT NOT NULL,
    artist_norm TEXT NOT NULL UNIQUE,
    track_count INTEGER NOT NULL,
    is_featured_only INTEGER NOT NULL DEFAULT 0,
    cap INTEGER NOT NULL,
    disabled INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS expansion_candidates (
    id INTEGER PRIMARY KEY,
    artist_id INTEGER NOT NULL REFERENCES expansion_artists(id),
    candidate_id INTEGER NOT NULL REFERENCES candidate_charts(id),
    score REAL NOT NULL,
    breakdown_json TEXT NOT NULL,
    UNIQUE (artist_id, candidate_id)
);

CREATE TABLE IF NOT EXISTS selections (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    track_id INTEGER REFERENCES spotify_tracks(id),
    artist_norm TEXT,
    candidate_id INTEGER NOT NULL REFERENCES candidate_charts(id),
    priority_tier INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'selected',
    score REAL NOT NULL,
    explanation TEXT,
    planned_bytes INTEGER NOT NULL DEFAULT 0,
    target_relpath TEXT,
    UNIQUE (kind, candidate_id)
);

CREATE TABLE IF NOT EXISTS downloads (
    id INTEGER PRIMARY KEY,
    -- Keyed by candidate (not selection) so download state survives re-plans:
    -- candidate rows persist across rebuilds, selections do not.
    candidate_id INTEGER NOT NULL REFERENCES candidate_charts(id) UNIQUE,
    status TEXT NOT NULL,
    bytes INTEGER NOT NULL DEFAULT 0,
    path TEXT,
    md5 TEXT,
    sha256 TEXT,
    validation_json TEXT NOT NULL DEFAULT '{}',
    downloaded_at REAL,
    error TEXT
);

CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY,
    stage TEXT NOT NULL,
    message TEXT NOT NULL,
    detail TEXT,
    occurred_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS manual_decisions (
    id INTEGER PRIMARY KEY,
    track_key TEXT NOT NULL,
    source TEXT NOT NULL,
    chart_id INTEGER NOT NULL,
    decision TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE (track_key, source, chart_id)
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Spotify/library state is deliberately separate from the legacy CSV tables.
-- All new relationships use additive tables so old databases can be opened
-- without rewriting or deleting their existing state.
CREATE TABLE IF NOT EXISTS spotify_account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    spotify_user_id TEXT,
    user_id TEXT,
    display_name TEXT,
    scopes_json TEXT NOT NULL DEFAULT '[]',
    scopes TEXT NOT NULL DEFAULT '[]',
    country TEXT,
    product TEXT,
    href TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS spotify_playlists (
    id INTEGER PRIMARY KEY,
    spotify_id TEXT NOT NULL UNIQUE,
    playlist_id TEXT,
    name TEXT NOT NULL DEFAULT '',
    url TEXT,
    owner_id TEXT,
    owner_name TEXT,
    is_owned INTEGER,
    collaborative INTEGER,
    public INTEGER,
    access_status TEXT NOT NULL DEFAULT 'accessible',
    access_reason TEXT,
    snapshot_id TEXT,
    item_count INTEGER,
    artwork_url TEXT,
    artwork_id TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS spotify_playlist_tracks (
    id INTEGER PRIMARY KEY,
    playlist_id INTEGER NOT NULL REFERENCES spotify_playlists(id) ON DELETE CASCADE,
    spotify_playlist_id TEXT NOT NULL,
    track_id INTEGER REFERENCES spotify_tracks(id) ON DELETE SET NULL,
    spotify_track_id TEXT NOT NULL,
    position INTEGER,
    added_at TEXT,
    status TEXT NOT NULL DEFAULT 'imported',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (playlist_id, spotify_track_id)
);

CREATE VIEW IF NOT EXISTS spotify_playlist_memberships AS
    SELECT * FROM spotify_playlist_tracks;

CREATE VIEW IF NOT EXISTS playlist_memberships AS
    SELECT * FROM spotify_playlist_tracks;

CREATE TABLE IF NOT EXISTS spotify_liked_tracks (
    track_id INTEGER PRIMARY KEY REFERENCES spotify_tracks(id) ON DELETE CASCADE,
    spotify_track_id TEXT NOT NULL UNIQUE,
    added_at TEXT,
    status TEXT NOT NULL DEFAULT 'imported',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS spotify_item_statuses (
    id INTEGER PRIMARY KEY,
    workspace_id INTEGER REFERENCES discovery_workspaces(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    container_id TEXT NOT NULL DEFAULT '',
    item_key TEXT NOT NULL,
    spotify_id TEXT,
    title TEXT,
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    position INTEGER,
    detail_json TEXT NOT NULL DEFAULT '{}',
    occurred_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (workspace_id, source, container_id, item_key)
);

CREATE TABLE IF NOT EXISTS discovery_workspaces (
    id INTEGER PRIMARY KEY,
    workspace_key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'exact',
    status TEXT NOT NULL DEFAULT 'created',
    phase TEXT NOT NULL DEFAULT 'idle',
    spotify_account_id INTEGER REFERENCES spotify_account(id),
    total_tasks INTEGER NOT NULL DEFAULT 0,
    completed_tasks INTEGER NOT NULL DEFAULT 0,
    failed_tasks INTEGER NOT NULL DEFAULT 0,
    total_tracks INTEGER NOT NULL DEFAULT 0,
    completed_tracks INTEGER NOT NULL DEFAULT 0,
    source_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    progress_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    completed_at REAL,
    library_matching_fingerprint TEXT NOT NULL DEFAULT '',
    candidate_revision INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS discovery_scans (
    id INTEGER PRIMARY KEY,
    workspace_id INTEGER NOT NULL REFERENCES discovery_workspaces(id) ON DELETE CASCADE,
    scan_key TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'exact',
    status TEXT NOT NULL DEFAULT 'created',
    started_at REAL,
    finished_at REAL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (workspace_id, scan_key)
);

CREATE TABLE IF NOT EXISTS discovery_tasks (
    id INTEGER PRIMARY KEY,
    workspace_id INTEGER NOT NULL REFERENCES discovery_workspaces(id) ON DELETE CASCADE,
    scan_id INTEGER REFERENCES discovery_scans(id) ON DELETE SET NULL,
    source TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'song',
    query_key TEXT NOT NULL DEFAULT '',
    query_text TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    total INTEGER NOT NULL DEFAULT 0,
    completed INTEGER NOT NULL DEFAULT 0,
    result_cap INTEGER,
    source_total INTEGER,
    returned_count INTEGER NOT NULL DEFAULT 0,
    cap_reached INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    UNIQUE (workspace_id, source, kind, query_key)
);

CREATE TABLE IF NOT EXISTS discovery_progress (
    workspace_id INTEGER PRIMARY KEY REFERENCES discovery_workspaces(id) ON DELETE CASCADE,
    phase TEXT NOT NULL DEFAULT 'idle',
    status TEXT NOT NULL DEFAULT 'created',
    total_tasks INTEGER NOT NULL DEFAULT 0,
    completed_tasks INTEGER NOT NULL DEFAULT 0,
    failed_tasks INTEGER NOT NULL DEFAULT 0,
    total_tracks INTEGER NOT NULL DEFAULT 0,
    completed_tracks INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    error TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS workspace_candidates (
    id INTEGER PRIMARY KEY,
    workspace_id INTEGER NOT NULL REFERENCES discovery_workspaces(id) ON DELETE CASCADE,
    track_id INTEGER REFERENCES spotify_tracks(id) ON DELETE CASCADE,
    candidate_id INTEGER NOT NULL REFERENCES candidate_charts(id) ON DELETE CASCADE,
    kind TEXT NOT NULL DEFAULT 'exact',
    artist_norm TEXT,
    task_id INTEGER REFERENCES discovery_tasks(id) ON DELETE SET NULL,
    score REAL NOT NULL DEFAULT 0,
    quality REAL NOT NULL DEFAULT 0,
    decision TEXT NOT NULL DEFAULT '',
    eligibility TEXT NOT NULL DEFAULT '',
    breakdown_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (workspace_id, track_id, candidate_id, kind, artist_norm)
);

CREATE TABLE IF NOT EXISTS workspace_task_candidates (
    task_id INTEGER NOT NULL REFERENCES discovery_tasks(id) ON DELETE CASCADE,
    candidate_id INTEGER NOT NULL REFERENCES candidate_charts(id) ON DELETE CASCADE,
    UNIQUE (task_id, candidate_id)
);

CREATE VIEW IF NOT EXISTS discovery_candidates AS
    SELECT * FROM workspace_candidates;

CREATE TABLE IF NOT EXISTS workspace_selections (
    id INTEGER PRIMARY KEY,
    workspace_id INTEGER NOT NULL REFERENCES discovery_workspaces(id) ON DELETE CASCADE,
    kind TEXT NOT NULL DEFAULT 'exact',
    track_id INTEGER REFERENCES spotify_tracks(id) ON DELETE CASCADE,
    spotify_track_id TEXT,
    artist_norm TEXT,
    candidate_id INTEGER NOT NULL REFERENCES candidate_charts(id) ON DELETE CASCADE,
    score REAL,
    explanation TEXT NOT NULL DEFAULT '',
    eligibility TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'selected',
    planned_bytes INTEGER NOT NULL DEFAULT 0,
    target_relpath TEXT,
    selected_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS workspace_expansion_artists (
    id INTEGER PRIMARY KEY,
    workspace_id INTEGER NOT NULL REFERENCES discovery_workspaces(id) ON DELETE CASCADE,
    artist_name TEXT NOT NULL,
    artist_norm TEXT NOT NULL,
    track_count INTEGER NOT NULL DEFAULT 0,
    cap INTEGER NOT NULL DEFAULT 0,
    selected INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (workspace_id, artist_norm)
);

CREATE TABLE IF NOT EXISTS matching_song_cache (
    fingerprint TEXT PRIMARY KEY,
    track_key TEXT NOT NULL,
    classification TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_matching_song_cache_track ON matching_song_cache(track_key);
CREATE TABLE IF NOT EXISTS matching_scan_cache (
    workspace_id INTEGER PRIMARY KEY REFERENCES discovery_workspaces(id) ON DELETE CASCADE,
    fingerprint TEXT NOT NULL,
    result_json TEXT NOT NULL,
    track_count INTEGER NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    candidate_revision INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS discovery_song_progress (
    workspace_id INTEGER NOT NULL REFERENCES discovery_workspaces(id) ON DELETE CASCADE,
    track_key TEXT NOT NULL,
    artist TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'waiting',
    candidate_count INTEGER NOT NULL DEFAULT 0,
    provenance TEXT NOT NULL DEFAULT '',
    classification TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL,
    PRIMARY KEY (workspace_id, track_key)
);
CREATE INDEX IF NOT EXISTS idx_discovery_song_progress_lookup
    ON discovery_song_progress(workspace_id, artist, title);

CREATE TABLE IF NOT EXISTS library_sources (
    id INTEGER PRIMARY KEY,
    workspace_id INTEGER NOT NULL REFERENCES discovery_workspaces(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    source_key TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    scope_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(workspace_id, provider, source_key)
);
CREATE TABLE IF NOT EXISTS source_imports (
    id INTEGER PRIMARY KEY,
    workspace_id INTEGER NOT NULL REFERENCES discovery_workspaces(id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES library_sources(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'complete',
    started_at REAL NOT NULL,
    completed_at REAL,
    total_rows INTEGER NOT NULL DEFAULT 0,
    accepted_rows INTEGER NOT NULL DEFAULT 0,
    skipped_rows INTEGER NOT NULL DEFAULT 0,
    duplicate_rows INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    report_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS library_tracks (
    id INTEGER PRIMARY KEY,
    workspace_id INTEGER NOT NULL REFERENCES discovery_workspaces(id) ON DELETE CASCADE,
    normalized_artist TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    title TEXT NOT NULL,
    artist TEXT NOT NULL,
    album TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER,
    isrc TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    liked INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(workspace_id, normalized_artist, normalized_title)
);
CREATE TABLE IF NOT EXISTS library_track_provenance (
    id INTEGER PRIMARY KEY,
    library_track_id INTEGER NOT NULL REFERENCES library_tracks(id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES library_sources(id) ON DELETE CASCADE,
    source_import_id INTEGER NOT NULL REFERENCES source_imports(id) ON DELETE CASCADE,
    provider_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    artist TEXT NOT NULL DEFAULT '',
    album TEXT NOT NULL DEFAULT '',
    isrc TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    liked INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    imported_at REAL NOT NULL,
    UNIQUE(source_id, provider_id)
);
CREATE TABLE IF NOT EXISTS library_playlists (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES library_sources(id) ON DELETE CASCADE,
    provider_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    liked INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    updated_at REAL NOT NULL,
    UNIQUE(source_id, provider_id)
);
CREATE TABLE IF NOT EXISTS library_track_playlists (
    library_track_id INTEGER NOT NULL REFERENCES library_tracks(id) ON DELETE CASCADE,
    playlist_id INTEGER NOT NULL REFERENCES library_playlists(id) ON DELETE CASCADE,
    position INTEGER,
    PRIMARY KEY(library_track_id, playlist_id)
);
CREATE INDEX IF NOT EXISTS idx_library_sources_workspace ON library_sources(workspace_id);
CREATE INDEX IF NOT EXISTS idx_library_tracks_workspace ON library_tracks(workspace_id);

CREATE TABLE IF NOT EXISTS workspace_expansion_batches (
    id INTEGER PRIMARY KEY,
    workspace_id INTEGER NOT NULL REFERENCES discovery_workspaces(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    phase TEXT NOT NULL DEFAULT 'preview',
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    selection_fingerprint TEXT NOT NULL,
    total_artists INTEGER NOT NULL DEFAULT 0 CHECK (total_artists BETWEEN 0 AND 25),
    completed_artists INTEGER NOT NULL DEFAULT 0,
    failed_artists INTEGER NOT NULL DEFAULT 0,
    skipped_artists INTEGER NOT NULL DEFAULT 0,
    network_requests INTEGER NOT NULL DEFAULT 0,
    current_artist TEXT NOT NULL DEFAULT '',
    heartbeat_at REAL NOT NULL,
    created_at REAL NOT NULL,
    started_at REAL,
    completed_at REAL,
    updated_at REAL NOT NULL,
    error TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (workspace_id, run_id)
);

CREATE TABLE IF NOT EXISTS workspace_expansion_batch_artists (
    id INTEGER PRIMARY KEY,
    batch_id INTEGER NOT NULL REFERENCES workspace_expansion_batches(id) ON DELETE CASCADE,
    expansion_artist_id INTEGER REFERENCES workspace_expansion_artists(id) ON DELETE SET NULL,
    artist_name TEXT NOT NULL,
    artist_norm TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    preview_status TEXT NOT NULL DEFAULT 'pending',
    search_status TEXT NOT NULL DEFAULT 'pending',
    preview_requests INTEGER NOT NULL DEFAULT 0,
    search_requests INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    started_at REAL,
    completed_at REAL,
    updated_at REAL NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (batch_id, artist_norm),
    UNIQUE (batch_id, ordinal)
);

CREATE TABLE IF NOT EXISTS workspace_scope (
    workspace_id INTEGER PRIMARY KEY REFERENCES discovery_workspaces(id) ON DELETE CASCADE,
    playlist_ids_json TEXT NOT NULL DEFAULT '[]',
    include_liked INTEGER NOT NULL DEFAULT 0,
    track_ids_json TEXT NOT NULL DEFAULT '[]',
    fingerprint TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS workspace_library_tracks (
    workspace_id INTEGER NOT NULL REFERENCES discovery_workspaces(id) ON DELETE CASCADE,
    track_id INTEGER NOT NULL REFERENCES spotify_tracks(id) ON DELETE CASCADE,
    already_owned INTEGER NOT NULL DEFAULT 0,
    owned_count INTEGER NOT NULL DEFAULT 0,
    membership_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (workspace_id, track_id)
);

CREATE TABLE IF NOT EXISTS inventory_roots (
    id INTEGER PRIMARY KEY,
    root_path TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'created',
    scanned_at REAL,
    file_count INTEGER NOT NULL DEFAULT 0,
    valid_count INTEGER NOT NULL DEFAULT 0,
    invalid_count INTEGER NOT NULL DEFAULT 0,
    diagnostic_count INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS inventory_charts (
    id INTEGER PRIMARY KEY,
    root_id INTEGER NOT NULL REFERENCES inventory_roots(id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    mtime_ns INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT,
    artist TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    album TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER,
    artist_norm TEXT NOT NULL DEFAULT '',
    title_norm TEXT NOT NULL DEFAULT '',
    validation_state TEXT NOT NULL DEFAULT 'unknown',
    parser_source TEXT NOT NULL DEFAULT '',
    diagnostics_json TEXT NOT NULL DEFAULT '[]',
    scanned_at REAL NOT NULL,
    UNIQUE (root_id, relative_path)
);

CREATE TABLE IF NOT EXISTS export_records (
    id INTEGER PRIMARY KEY,
    workspace_id INTEGER REFERENCES discovery_workspaces(id) ON DELETE SET NULL,
    export_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL DEFAULT 'folder',
    destination TEXT NOT NULL DEFAULT '',
    manifest_path TEXT,
    status TEXT NOT NULL DEFAULT 'created',
    managed INTEGER NOT NULL DEFAULT 0,
    manifest_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    completed_at REAL
);

CREATE TABLE IF NOT EXISTS export_items (
    id INTEGER PRIMARY KEY,
    export_id INTEGER NOT NULL REFERENCES export_records(id) ON DELETE CASCADE,
    selection_id INTEGER REFERENCES workspace_selections(id) ON DELETE SET NULL,
    source TEXT NOT NULL DEFAULT '',
    chart_id TEXT,
    source_hash TEXT,
    source_hash_algorithm TEXT,
    artifact_key TEXT,
    target_relpath TEXT NOT NULL DEFAULT '',
    path TEXT,
    sha256 TEXT,
    bytes INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    managed INTEGER NOT NULL DEFAULT 1,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (export_id, source, chart_id, target_relpath)
);

CREATE VIEW IF NOT EXISTS export_record_items AS
    SELECT * FROM export_items;

CREATE VIEW IF NOT EXISTS managed_export_files AS
    SELECT * FROM export_items WHERE managed = 1;

CREATE TABLE IF NOT EXISTS export_manifests (
    id INTEGER PRIMARY KEY,
    export_id INTEGER NOT NULL REFERENCES export_records(id) ON DELETE CASCADE,
    app TEXT NOT NULL DEFAULT 'csv-to-clonehero',
    format TEXT NOT NULL DEFAULT 'csv-to-clonehero-manifest',
    version INTEGER NOT NULL DEFAULT 1,
    destination TEXT NOT NULL DEFAULT '',
    generated_at TEXT,
    manifest_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    UNIQUE (export_id)
);

CREATE INDEX IF NOT EXISTS idx_spotify_playlists_name ON spotify_playlists(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_spotify_playlist_tracks_track ON spotify_playlist_tracks(spotify_track_id);
CREATE INDEX IF NOT EXISTS idx_spotify_item_statuses_item ON spotify_item_statuses(spotify_id);
CREATE INDEX IF NOT EXISTS idx_discovery_tasks_workspace ON discovery_tasks(workspace_id, status);
CREATE INDEX IF NOT EXISTS idx_workspace_candidates_track ON workspace_candidates(workspace_id, track_id);
CREATE INDEX IF NOT EXISTS idx_workspace_selections_workspace ON workspace_selections(workspace_id, status);
CREATE INDEX IF NOT EXISTS idx_workspace_library_tracks_workspace ON workspace_library_tracks(workspace_id, already_owned);
CREATE INDEX IF NOT EXISTS idx_expansion_batches_workspace ON workspace_expansion_batches(workspace_id, status);
CREATE INDEX IF NOT EXISTS idx_expansion_batch_artists_status ON workspace_expansion_batch_artists(batch_id, preview_status, search_status);
CREATE INDEX IF NOT EXISTS idx_inventory_charts_identity ON inventory_charts(root_id, artist_norm, title_norm);
CREATE INDEX IF NOT EXISTS idx_inventory_charts_freshness ON inventory_charts(root_id, relative_path, mtime_ns, size_bytes);
CREATE INDEX IF NOT EXISTS idx_export_items_managed ON export_items(managed, target_relpath);
CREATE INDEX IF NOT EXISTS idx_candidate_charts_source_chart ON candidate_charts(source, chart_id);

-- ---- multi-user (cloud product) -------------------------------------------
-- Per-user identity is the Spotify login. No passwords are stored. Each user's
-- Spotify refresh token is stored only here, encrypted with a server-side
-- Fernet key (TOKEN_ENCRYPTION_KEY), never in the OS keyring and never in git.
-- The 7-day query cache and candidate_charts stay GLOBAL on purpose: Enchor is
-- rate-limited per shared IP, so a shared cache is the main way a public app
-- stays under the ~1-2 req/s ceiling.
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    spotify_user_id TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT '',
    product TEXT NOT NULL DEFAULT '',
    href TEXT NOT NULL DEFAULT '',
    scopes_json TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS user_sessions (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0,
    user_agent TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS user_tokens (
    -- Fernet token is self-describing (includes its own IV), so one column.
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    encrypted_refresh_token TEXT NOT NULL DEFAULT '',
    scope_json TEXT NOT NULL DEFAULT '[]',
    rotate_version INTEGER NOT NULL DEFAULT 1,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS user_emails (
    -- Notifications only; never an account identity.
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    email TEXT NOT NULL DEFAULT '',
    verified INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS background_jobs (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    phase TEXT NOT NULL DEFAULT 'queued',
    message TEXT NOT NULL DEFAULT '',
    error TEXT,
    workspace_key TEXT NOT NULL DEFAULT '',
    progress_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    notify_email TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    heartbeat_at REAL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_tokens (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    encrypted_refresh_token TEXT NOT NULL DEFAULT '',
    scope_json TEXT NOT NULL DEFAULT '[]',
    rotate_version INTEGER NOT NULL DEFAULT 1,
    updated_at REAL NOT NULL,
    PRIMARY KEY (user_id, provider)
);

CREATE TABLE IF NOT EXISTS source_preflights (
    source_name TEXT PRIMARY KEY,
    endpoint TEXT NOT NULL,
    request_version TEXT NOT NULL,
    checked_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    success INTEGER NOT NULL DEFAULT 0,
    supported INTEGER NOT NULL DEFAULT 0,
    authoritative INTEGER NOT NULL DEFAULT 0,
    found INTEGER,
    out_of INTEGER,
    returned_count INTEGER NOT NULL DEFAULT 0,
    page INTEGER,
    error TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS chart_artifact_validations (
    source TEXT NOT NULL,
    chart_id INTEGER NOT NULL,
    catalog_snapshot_id TEXT NOT NULL DEFAULT '',
    catalog_row_hash TEXT NOT NULL DEFAULT '',
    artifact_ref TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'UNVERIFIED'
        CHECK(state IN ('VERIFIED', 'UNVERIFIED', 'REJECTED', 'UNAVAILABLE')),
    validator_version TEXT NOT NULL DEFAULT '',
    artifact_sha256 TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    format TEXT NOT NULL DEFAULT '',
    playable_note_count INTEGER NOT NULL DEFAULT 0,
    validation_json TEXT NOT NULL DEFAULT '{}',
    reason_code TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    http_status INTEGER,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    checked_at REAL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(source, chart_id)
);
CREATE INDEX IF NOT EXISTS idx_chart_artifact_validations_state
    ON chart_artifact_validations(state);

CREATE TABLE IF NOT EXISTS chart_artifact_validation_history (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    chart_id INTEGER NOT NULL,
    catalog_snapshot_id TEXT NOT NULL DEFAULT '',
    catalog_row_hash TEXT NOT NULL DEFAULT '',
    artifact_ref TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    validator_version TEXT NOT NULL DEFAULT '',
    artifact_sha256 TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    format TEXT NOT NULL DEFAULT '',
    playable_note_count INTEGER NOT NULL DEFAULT 0,
    validation_json TEXT NOT NULL DEFAULT '{}',
    reason_code TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    http_status INTEGER,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    checked_at REAL,
    updated_at REAL NOT NULL,
    archived_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chart_artifact_validation_history_identity
    ON chart_artifact_validation_history(source, chart_id, id);

CREATE INDEX IF NOT EXISTS idx_user_sessions_expires ON user_sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_background_jobs_user ON background_jobs(user_id, status);
"""


def _as_mapping(value: Any) -> dict[str, Any]:
    """Normalize mappings and dataclasses without importing integration code."""
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    values = getattr(value, "__dict__", None)
    if isinstance(values, dict):
        return dict(values)
    return {}


def _catalog_candidate_row_hash(value: Any) -> str:
    supplied = str(getattr(value, "_catalog_row_hash", "") or "")
    if supplied:
        return supplied
    data = _as_mapping(value)
    for field_name in (
        "db_id", "artwork_url", "artwork_id", "issues",
        "diff_guitarghl_coop", "diff_rhythmghl",
    ):
        data.pop(field_name, None)
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _first_value(data: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in data and data[name] is not None:
            return data[name]
    return default


def _json_text(value: Any, default: Any) -> str:
    if value is None:
        value = default
    try:
        return json.dumps(value, ensure_ascii=True, default=str)
    except (TypeError, ValueError):
        return json.dumps(default, ensure_ascii=True)


def _load_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return parsed


def _candidate_to_json(candidate: ChartCandidate) -> str:
    data = {
        k: v
        for k, v in vars(candidate).items()
        if k != "db_id"
    }
    # These are compatibility aliases reconstructed by ChartCandidate on
    # load. Persisting both copies multiplied large Enchor issue payloads.
    for alias in ("artwork_url", "artwork_id", "issues", "difficulties"):
        data.pop(alias, None)
    return _json_text(data, {})


def _candidate_from_json(db_id: int, text: str) -> ChartCandidate:
    data = json.loads(text)
    data.pop("db_id", None)
    # A cache can outlive a model release.  Ignore fields newer than the
    # currently installed dataclass while retaining all fields it understands.
    allowed = {item.name for item in fields(ChartCandidate)}
    candidate = ChartCandidate(**{k: v for k, v in data.items() if k in allowed})
    candidate.db_id = db_id
    return candidate


def _coerce_candidate(value: Any) -> ChartCandidate:
    if isinstance(value, ChartCandidate):
        return value
    data = _as_mapping(value)
    aliases = {
        "chartId": "chart_id",
        "songId": "song_id",
        "versionId": "version_id",
        "groupId": "version_group_id",
        "versionGroupId": "version_group_id",
        "albumArtMd5": "source_artwork_id",
        "artworkId": "source_artwork_id",
        "artworkUrl": "source_artwork_url",
        "sourceArtworkUrl": "source_artwork_url",
        "sourceArtworkId": "source_artwork_id",
        "reviewUrl": "review_url",
        "reviewStatus": "review_status",
        "issueSeverity": "issue_severity",
        "sourceTotal": "source_total",
        "returnedCount": "returned_count",
        "capReached": "cap_reached",
    }
    for old, new in aliases.items():
        if new not in data and old in data:
            data[new] = data[old]
    allowed = {item.name for item in fields(ChartCandidate)}
    required = {
        "source": "",
        "chart_id": 0,
        "md5": "",
        "name": "",
        "artist": "",
        "name_norm": "",
        "artist_norm": "",
    }
    required.update({key: data.get(key, default) for key, default in required.items()})
    candidate = ChartCandidate(
        **{key: data.get(key, default) for key, default in required.items()},
        **{key: value for key, value in data.items() if key in allowed and key not in required},
    )
    db_id = data.get("db_id") or data.get("id")
    if db_id is not None:
        try:
            candidate.db_id = int(db_id)
        except (TypeError, ValueError):
            pass
    return candidate


def _normalise_spotify_track(value: Any) -> tuple[Track, list[dict[str, Any]], bool, str | None]:
    """Convert a Spotify record or plain payload into the legacy Track model."""
    if isinstance(value, Track):
        return value, [], bool(getattr(value, "liked", False)), getattr(value, "added_at", None)
    data = _as_mapping(value)
    nested_track = data.get("track")
    if isinstance(nested_track, Mapping):
        wrapper = dict(data)
        data = dict(nested_track)
        for key in ("added_at", "is_local", "position", "playlist", "playlists"):
            if key in wrapper and key not in data:
                data[key] = wrapper[key]
    spotify_id = str(_first_value(data, "spotify_id", "track_id", "id", default="") or "")
    title = str(_first_value(data, "title", "name", default="") or "")
    raw_artists = _first_value(data, "artists", default=None)
    artist_names: list[str] = []
    if isinstance(raw_artists, (list, tuple)):
        for artist in raw_artists:
            if isinstance(artist, Mapping):
                name = artist.get("name")
            else:
                name = artist
            if name:
                artist_names.append(str(name).strip())
    artist_full = str(
        _first_value(data, "artist_full", "artist", "primary_artist", default="") or ""
    ).strip()
    if not artist_full and artist_names:
        artist_full = ", ".join(artist_names)
    if not artist_names and artist_full:
        artist_names = [artist_full]
    album = str(_first_value(data, "album", default="") or "")
    album_norm = str(_first_value(data, "album_norm", default="") or "")
    title_norm = str(_first_value(data, "title_norm", default="") or "")
    artist_norm = str(_first_value(data, "artist_norm", default="") or "")
    primary_artist = str(
        _first_value(data, "primary_artist", default=artist_names[0] if artist_names else "")
        or ""
    )
    primary_artist_norm = str(
        _first_value(data, "primary_artist_norm", default="") or ""
    )
    if not title_norm or not artist_norm or not primary_artist_norm or (album and not album_norm):
        from csv_to_clonehero.normalization import (
            make_track_key,
            normalize_artist,
            normalize_title,
            split_artists,
        )

        if not title_norm:
            title_norm = normalize_title(title)
        if not artist_norm:
            artist_norm = normalize_artist(artist_full)
        if not primary_artist:
            primary_artist, featured = split_artists(artist_full)
        else:
            _, featured = split_artists(artist_full)
        if not primary_artist_norm:
            primary_artist_norm = normalize_artist(primary_artist)
        if album and not album_norm:
            album_norm = normalize_title(album)
        fallback_key = make_track_key(spotify_id, primary_artist_norm, title_norm)
    else:
        fallback_key = f"spotify:{spotify_id}" if spotify_id else f"{primary_artist_norm}|{title_norm}"
        from csv_to_clonehero.normalization import split_artists

        _, featured = split_artists(artist_full)
    provided_featured = _first_value(data, "featured_artists", default=None)
    if isinstance(provided_featured, (list, tuple)):
        featured = [str(item) for item in provided_featured]
    playlists_value = _first_value(data, "playlists", default=())
    playlist_values = playlists_value if isinstance(playlists_value, (list, tuple, set)) else ()
    playlists = [_normalise_spotify_playlist(item) for item in playlist_values]
    playlists = [item for item in playlists if item.get("spotify_id")]
    origin_metadata = _first_value(data, "origin_metadata", "metadata", default={})
    if not isinstance(origin_metadata, Mapping):
        origin_metadata = {"value": origin_metadata}
    track = Track(
        track_key=str(_first_value(data, "track_key", default=fallback_key) or fallback_key),
        title=title,
        title_norm=title_norm,
        artist_full=artist_full,
        artist_norm=artist_norm,
        primary_artist=primary_artist,
        primary_artist_norm=primary_artist_norm,
        featured_artists=featured,
        album=album,
        album_norm=album_norm,
        spotify_id=spotify_id,
        playlists=[str(item.get("name") or item["spotify_id"]) for item in playlists],
        db_id=data.get("db_id"),
        album_id=str(_first_value(data, "album_id", "spotify_album_id", default="") or ""),
        duration_ms=_first_value(data, "duration_ms", default=None),
        isrc=str(_first_value(data, "isrc", default="") or ""),
        spotify_url=str(_first_value(data, "spotify_url", "url", default="") or ""),
        added_at=_first_value(data, "added_at", default=None),
        origin=str(_first_value(data, "origin", default="spotify" if spotify_id else "") or ""),
        origin_metadata=dict(origin_metadata),
    )
    if spotify_id:
        track.origin_metadata.setdefault("source", "spotify")
        if playlists:
            track.origin_metadata["playlist_ids"] = [item["spotify_id"] for item in playlists]
        if bool(_first_value(data, "liked", default=False)):
            track.origin_metadata["liked"] = True
    return (
        track,
        playlists,
        bool(_first_value(data, "liked", default=False)),
        track.added_at,
    )


def _normalise_spotify_playlist(value: Any) -> dict[str, Any]:
    data = _as_mapping(value)
    playlist_id = str(
        _first_value(data, "spotify_id", "playlist_id", "id", default="") or ""
    ).strip()
    external_urls = data.get("external_urls")
    if isinstance(external_urls, Mapping):
        url = str(external_urls.get("spotify") or data.get("href") or "")
    else:
        url = str(_first_value(data, "url", "href", default="") or "")
    artwork_url = str(
        _first_value(data, "artwork_url", "image_url", default="") or ""
    )
    images = data.get("images")
    if not artwork_url and isinstance(images, (list, tuple)):
        for image in images:
            if isinstance(image, Mapping) and image.get("url"):
                artwork_url = str(image["url"])
                break
    owner = data.get("owner") if isinstance(data.get("owner"), Mapping) else {}
    tracks = data.get("tracks") if isinstance(data.get("tracks"), Mapping) else {}
    items = data.get("items") if isinstance(data.get("items"), Mapping) else {}
    total_tracks = _first_value(data, "total_tracks", "item_count", default=items.get("total"))
    if total_tracks is None:
        total_tracks = tracks.get("total")
    try:
        total_tracks = int(total_tracks) if total_tracks is not None else None
    except (TypeError, ValueError):
        total_tracks = None
    access_status = str(
        _first_value(data, "access_status", "status", default="accessible") or "accessible"
    )
    if data.get("accessible") is False:
        access_status = "inaccessible"
    # Keep metadata useful for diagnostics without recursively embedding a
    # previously-normalized playlist every time the snapshot is upserted.
    metadata = data.get("metadata")
    if isinstance(metadata, Mapping):
        metadata = dict(metadata)
    else:
        metadata = {
            key: value
            for key, value in data.items()
            if key not in {"metadata", "tracks", "items", "images"}
        }
    return {
        "spotify_id": playlist_id,
        "playlist_id": playlist_id,
        "name": str(_first_value(data, "name", default="") or ""),
        "url": url,
        "owner_id": str(_first_value(data, "owner_id", default=owner.get("id", "")) or ""),
        "owner_name": str(
            _first_value(data, "owner_name", default=owner.get("display_name", "")) or ""
        ),
        "is_owned": _first_value(data, "is_owned", "owned", default=None),
        "collaborative": _first_value(data, "collaborative", default=None),
        "public": _first_value(data, "public", default=None),
        "access_status": access_status,
        "access_reason": str(
            _first_value(data, "access_reason", "reason", "access_error", "error", default="")
            or ""
        ),
        "snapshot_id": str(_first_value(data, "snapshot_id", default="") or ""),
        "item_count": total_tracks,
        "artwork_url": artwork_url,
        "artwork_id": str(_first_value(data, "artwork_id", default="") or ""),
        "metadata": metadata,
    }


class Database:
    """SQLite gateway for both local state and public workspace state.

    Connections use foreign keys, a busy timeout, and WAL where supported.
    Callers should open one instance per thread and close it when the operation
    ends; the database is the only module that should own SQL persistence.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 30000")
        try:
            self._conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError:
            # Some read-only/network filesystems do not support WAL.  The
            # connection remains usable with the longer busy timeout.
            pass
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    def _migrate(self) -> None:
        current = 0
        schema_exists = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
        ).fetchone()
        if schema_exists:
            row = self._conn.execute("SELECT MAX(version) AS version FROM schema_version").fetchone()
            current = int((row["version"] if row else 0) or 0)
        if schema_exists and current >= SCHEMA_VERSION:
            return
        with self._conn:
            self._conn.executescript(SCHEMA)
            # ``CREATE TABLE IF NOT EXISTS`` does not add columns to a legacy
            # table.  Keep these ALTERs small, explicit, and idempotent so a
            # version-1 database retains every existing row and index.
            for table, column, definition in (
                ("spotify_tracks", "album_id", "TEXT"),
                ("spotify_tracks", "duration_ms", "INTEGER"),
                ("spotify_tracks", "isrc", "TEXT"),
                ("spotify_tracks", "spotify_url", "TEXT"),
                ("spotify_tracks", "origin", "TEXT"),
                ("spotify_tracks", "origin_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("spotify_tracks", "origin_metadata_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("spotify_account", "scopes", "TEXT NOT NULL DEFAULT '[]'"),
                ("spotify_tracks", "added_at", "TEXT"),
                ("spotify_tracks", "spotify_synced_at", "REAL"),
                ("search_queries", "source_name", "TEXT"),
                ("search_queries", "source_version", "TEXT"),
                ("search_queries", "result_cap", "INTEGER"),
                ("search_queries", "source_total", "INTEGER"),
                ("search_queries", "cap_reached", "INTEGER NOT NULL DEFAULT 0"),
                ("search_queries", "returned_count", "INTEGER NOT NULL DEFAULT 0"),
                ("search_queries", "complete", "INTEGER NOT NULL DEFAULT 1"),
                ("search_queries", "metadata_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("candidate_charts", "source_artwork_url", "TEXT"),
                ("candidate_charts", "source_artwork_id", "TEXT"),
                ("candidate_charts", "issue_details_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("candidate_charts", "issue_severity", "TEXT"),
                ("candidate_charts", "review_status", "TEXT"),
                ("candidate_charts", "review_url", "TEXT"),
                ("candidate_charts", "song_id", "TEXT"),
                ("candidate_charts", "version_id", "TEXT"),
                ("candidate_charts", "version_group_id", "TEXT"),
                ("candidate_charts", "group_id", "TEXT"),
                ("candidate_charts", "source_total", "INTEGER"),
                ("candidate_charts", "cap_reached", "INTEGER NOT NULL DEFAULT 0"),
                ("candidate_charts", "returned_count", "INTEGER"),
                ("candidate_charts", "source_metadata_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("candidate_charts", "diff_band", "INTEGER"),
                ("candidate_charts", "diff_guitar_coop", "INTEGER"),
                ("candidate_charts", "diff_rhythm", "INTEGER"),
                ("candidate_charts", "diff_drums_real", "INTEGER"),
                ("candidate_charts", "diff_guitar_coop_ghl", "INTEGER"),
                ("candidate_charts", "diff_rhythm_ghl", "INTEGER"),
                ("candidate_charts", "diff_bassghl", "INTEGER"),
                ("candidate_charts", "diff_guitar_ghl", "INTEGER"),
                ("candidate_charts", "diff_bass_ghl", "INTEGER"),
                ("candidate_charts", "diff_vocals_harm1", "INTEGER"),
                ("candidate_charts", "diff_vocals_harm2", "INTEGER"),
                ("candidate_charts", "diff_vocals_harm3", "INTEGER"),
                ("candidate_charts", "difficulty_fields_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("background_jobs", "cancel_requested", "INTEGER NOT NULL DEFAULT 0"),
                ("background_jobs", "heartbeat_at", "REAL"),
                ("downloads", "sha256", "TEXT"),
                ("downloads", "validation_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("discovery_workspaces", "library_matching_fingerprint", "TEXT NOT NULL DEFAULT ''"),
                ("discovery_workspaces", "candidate_revision", "INTEGER NOT NULL DEFAULT 0"),
                ("matching_scan_cache", "candidate_revision", "INTEGER NOT NULL DEFAULT 0"),
                ("discovery_song_progress", "reason", "TEXT NOT NULL DEFAULT ''"),
                ("chart_artifact_validations", "catalog_snapshot_id", "TEXT NOT NULL DEFAULT ''"),
                ("chart_artifact_validations", "catalog_row_hash", "TEXT NOT NULL DEFAULT ''"),
            ):
                self._ensure_column(table, column, definition)
            self._conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_chart_artifact_validations_batch
                   ON chart_artifact_validations(
                       catalog_snapshot_id,state,source,checked_at,chart_id
                   )"""
            )
            # Older query rows predate the explicit returned-count field.  This
            # is intentionally version-gated: opening a current DB is read-only
            # during startup and cannot contend with a discovery worker.
            if current < 2:
                self._conn.execute(
                    "UPDATE search_queries SET returned_count = result_count "
                    "WHERE returned_count IS NULL OR returned_count = 0"
                )
            if current < 11:
                # Version 10 accidentally persisted the truncation marker as a
                # literal suffix. Normalize those user workspace keys while
                # retaining all workspace-scoped rows by updating the parent
                # key in place.
                self._conn.execute(
                    "UPDATE discovery_workspaces "
                    "SET workspace_key = substr(workspace_key, 1, length(workspace_key) - 5) "
                    "WHERE workspace_key LIKE 'spotify:u:%[:64]'"
                )
            row = self._conn.execute("SELECT COUNT(*) AS c FROM schema_version").fetchone()
            if row["c"] == 0:
                self._conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
                )
            elif current < SCHEMA_VERSION:
                self._conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        """Add one known schema column when opening a pre-Spotify database."""
        columns = {
            row["name"]
            for row in self._conn.execute(f"PRAGMA table_info({table})")
        }
        if column not in columns:
            self._conn.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
            )

    def refresh_library_matching_fingerprint(self, workspace_id: int | str) -> str:
        """Persist the canonical fields consumed by matching as one identity."""
        db_id = self._workspace_db_id(workspace_id)
        # Provider provenance/import metadata is intentionally excluded: it
        # does not affect retrieval or scoring and would make an equivalent
        # source reimport invalidate a valid completed scan.
        rows = self._conn.execute(
            """SELECT t.normalized_artist,t.normalized_title,t.title,t.artist,t.album,
                      t.duration_ms,t.isrc,t.url
                 FROM library_tracks t
                WHERE t.workspace_id = ?
                  AND EXISTS (
                        SELECT 1
                          FROM library_track_provenance p
                          JOIN library_sources s ON s.id = p.source_id
                         WHERE p.library_track_id = t.id
                           AND p.source_import_id = (
                                 SELECT MAX(i.id)
                                   FROM source_imports i
                                  WHERE i.source_id = s.id
                           )
                  )
                ORDER BY t.normalized_artist,t.normalized_title""",
            (db_id,),
        )
        digest = hashlib.sha256()
        for row in rows:
            digest.update(json.dumps(tuple(row), sort_keys=True, default=str).encode("utf-8"))
            digest.update(b"\n")
        fingerprint = digest.hexdigest()
        with self._conn:
            self._conn.execute("UPDATE discovery_workspaces SET library_matching_fingerprint=?, updated_at=? WHERE id=?", (fingerprint, time.time(), db_id))
        return fingerprint

    def get_library_matching_fingerprint(self, workspace_id: int | str) -> str:
        db_id = self._workspace_db_id(workspace_id)
        row = self._conn.execute("SELECT library_matching_fingerprint FROM discovery_workspaces WHERE id=?", (db_id,)).fetchone()
        return str(row["library_matching_fingerprint"] or "") if row else ""

    # ------------------------------------------------------------------ meta
    def set_meta(self, key: str, value: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_meta(self, key: str, default: str = "") -> str:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    # --------------------------------------------------- neutral library store
    def upsert_library_snapshot(
        self,
        workspace_id: int | str,
        snapshot: Any,
        *,
        scope: Mapping[str, Any] | None = None,
        on_progress: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Persist one provider snapshot without replacing other providers."""
        import time as _time

        from csv_to_clonehero.providers.base import provider_track_to_track

        db_id = self._workspace_db_id(workspace_id)
        if db_id is None:
            raise ValueError("workspace is required")
        now = _time.time()
        provider = str(snapshot.provider).strip().casefold()
        source_key = str((snapshot.metadata or {}).get("import", "api")).strip() or "api"
        report = snapshot.import_report.as_dict() if getattr(snapshot, "import_report", None) else {}
        with self._conn:
            source = self._conn.execute(
                "SELECT id FROM library_sources WHERE workspace_id=? AND provider=? AND source_key=?",
                (db_id, provider, source_key),
            ).fetchone()
            if source:
                source_id = int(source["id"])
                self._conn.execute(
                    "UPDATE library_sources SET status='active', scope_json=?, metadata_json=?, updated_at=? WHERE id=?",
                    (_json_text(scope or {}, {}), _json_text(snapshot.metadata or {}, {}), now, source_id),
                )
            else:
                cur = self._conn.execute(
                    "INSERT INTO library_sources(workspace_id,provider,source_key,status,scope_json,metadata_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (db_id, provider, source_key, "active", _json_text(scope or {}, {}), _json_text(snapshot.metadata or {}, {}), now, now),
                )
                source_id = int(cur.lastrowid)
            cur = self._conn.execute(
                "INSERT INTO source_imports(workspace_id,source_id,status,started_at,completed_at,total_rows,accepted_rows,skipped_rows,duplicate_rows,report_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (db_id, source_id, "complete", now, now, report.get("total_rows", len(snapshot.tracks)), report.get("accepted_rows", len(snapshot.tracks)), report.get("skipped_rows", 0), report.get("duplicate_rows", 0), _json_text(report, {})),
            )
            import_id = int(cur.lastrowid)
            legacy_tracks = []
            total_tracks = len(snapshot.tracks)
            if on_progress:
                on_progress({
                    "phase": "persist",
                    "message": f"Saving SoundCloud tracks (0 / {total_tracks})...",
                    "total_tracks": total_tracks,
                    "persisted_tracks": 0,
                })
            for position, value in enumerate(snapshot.tracks, start=1):
                track = provider_track_to_track(value)
                legacy_tracks.append(track)
                self._conn.execute(
                    """INSERT INTO library_tracks(workspace_id,normalized_artist,normalized_title,title,artist,album,duration_ms,isrc,url,liked,metadata_json,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(workspace_id,normalized_artist,normalized_title) DO UPDATE SET
                         title=excluded.title, artist=excluded.artist, album=CASE WHEN excluded.album!='' THEN excluded.album ELSE library_tracks.album END,
                         duration_ms=COALESCE(excluded.duration_ms,library_tracks.duration_ms), isrc=CASE WHEN excluded.isrc!='' THEN excluded.isrc ELSE library_tracks.isrc END,
                         url=CASE WHEN excluded.url!='' THEN excluded.url ELSE library_tracks.url END, liked=MAX(library_tracks.liked,excluded.liked),
                         metadata_json=excluded.metadata_json, updated_at=excluded.updated_at""",
                    (db_id, value.artist_norm, value.title_norm, value.title, value.artist, value.album, value.duration_ms, value.isrc, value.url, int(value.liked), _json_text(value.metadata, {}), now, now),
                )
                row = self._conn.execute(
                    "SELECT id FROM library_tracks WHERE workspace_id=? AND normalized_artist=? AND normalized_title=?",
                    (db_id, value.artist_norm, value.title_norm),
                ).fetchone()
                if row:
                    provider_id = str(value.provider_id or f"{value.artist_norm}|{value.title_norm}")
                    self._conn.execute(
                        """INSERT INTO library_track_provenance(library_track_id,source_id,source_import_id,provider_id,title,artist,album,isrc,url,liked,metadata_json,imported_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id,provider_id) DO UPDATE SET
                           library_track_id=excluded.library_track_id, source_import_id=excluded.source_import_id, title=excluded.title, artist=excluded.artist,
                           album=excluded.album, isrc=excluded.isrc, url=excluded.url, liked=excluded.liked, metadata_json=excluded.metadata_json, imported_at=excluded.imported_at""",
                         (int(row["id"]), source_id, import_id, provider_id, value.title, value.artist, value.album, value.isrc, value.url, int(value.liked), _json_text(value.metadata, {}), now),
                    )
                if on_progress and (position == total_tracks or position % 25 == 0):
                    on_progress({
                        "phase": "persist",
                        "message": f"Saving SoundCloud tracks ({position} / {total_tracks})...",
                        "total_tracks": total_tracks,
                        "persisted_tracks": position,
                    })
            # Keep the existing chart engine projection usable for all callers.
            self.upsert_spotify_tracks(legacy_tracks)
        library_fingerprint = self.refresh_library_matching_fingerprint(db_id)
        # A replacement import must remove tracks that disappeared from that
        # source while retaining tracks from every other current source.
        current_rows = self.get_neutral_library_tracks(db_id)
        engine_tracks = self.get_spotify_tracks(True)
        by_identity = {
            (track.primary_artist_norm, track.title_norm): track
            for track in engine_tracks
            if track.db_id is not None
        }
        by_identity.update({
            (track.artist_norm, track.title_norm): track
            for track in engine_tracks
            if track.db_id is not None
        })
        self.replace_workspace_library_tracks(
            db_id,
            [
                by_identity[(str(row["normalized_artist"]), str(row["normalized_title"]))]
                for row in current_rows
                if (str(row["normalized_artist"]), str(row["normalized_title"])) in by_identity
            ],
        )
        return {"provider": provider, "source_id": source_id, "import_id": import_id, "tracks": len(snapshot.tracks), "report": report, "library_matching_fingerprint": library_fingerprint}

    def get_neutral_library_tracks(self, workspace_id: int | str, *, provider: str | None = None) -> list[sqlite3.Row]:
        db_id = self._workspace_db_id(workspace_id)
        if db_id is None:
            return []
        params: list[Any] = [db_id]
        clause = ""
        if provider:
            clause = " AND EXISTS (SELECT 1 FROM library_track_provenance p JOIN library_sources s ON s.id=p.source_id WHERE p.library_track_id=t.id AND p.source_import_id=(SELECT MAX(i2.id) FROM source_imports i2 WHERE i2.source_id=s.id) AND s.provider=?)"
            params.append(str(provider).casefold())
        else:
            clause = " AND EXISTS (SELECT 1 FROM library_track_provenance p JOIN library_sources s ON s.id=p.source_id WHERE p.library_track_id=t.id AND p.source_import_id=(SELECT MAX(i2.id) FROM source_imports i2 WHERE i2.source_id=s.id))"
        return list(self._conn.execute("SELECT t.* FROM library_tracks t WHERE t.workspace_id=?" + clause + " ORDER BY t.artist,t.title", params))

    def get_source_summary(self, workspace_id: int | str, provider: str | None = None) -> list[sqlite3.Row]:
        db_id = self._workspace_db_id(workspace_id)
        if db_id is None:
            return []
        params: list[Any] = [db_id]
        clause = ""
        if provider:
            clause = " AND s.provider=?"
            params.append(str(provider).casefold())
        return list(self._conn.execute(
            """SELECT s.provider,s.source_key,s.status,s.updated_at,
                       COUNT(DISTINCT p.library_track_id) AS tracks,
                       COUNT(DISTINCT p.provider_id) AS provenance, i.skipped_rows AS skipped_rows,
                       i.duplicate_rows AS duplicate_rows, i.report_json AS report_json,
                       i.id AS import_id, i.status AS import_status, i.error AS import_error,
                       i.completed_at AS completed_at, i.total_rows AS total_rows, i.accepted_rows AS accepted_rows
                 FROM library_sources s
                 LEFT JOIN source_imports i ON i.id=(SELECT MAX(i2.id) FROM source_imports i2 WHERE i2.source_id=s.id)
                 LEFT JOIN library_track_provenance p ON p.source_id=s.id AND p.source_import_id=i.id
                 WHERE s.workspace_id=?""" + clause + " GROUP BY s.id ORDER BY s.provider,s.source_key", params))

    def get_combined_summary(self, workspace_id: int | str) -> dict[str, int]:
        rows = self.get_neutral_library_tracks(workspace_id)
        return {"tracks": len(rows), "artists": len({row["normalized_artist"] for row in rows}), "sources": len(self.get_source_summary(workspace_id))}

    def get_track_provenance(self, workspace_id: int | str) -> list[sqlite3.Row]:
        db_id = self._workspace_db_id(workspace_id)
        if db_id is None:
            return []
        return list(self._conn.execute(
            """SELECT t.normalized_artist,t.normalized_title,p.provider_id,s.provider,p.title,p.artist,p.album,p.liked,
                      p.isrc,p.url,p.metadata_json
               FROM library_tracks t JOIN library_track_provenance p ON p.library_track_id=t.id
               JOIN library_sources s ON s.id=p.source_id
               WHERE t.workspace_id=? AND p.source_import_id=(SELECT MAX(i.id) FROM source_imports i WHERE i.source_id=s.id)
               ORDER BY t.artist,t.title""",
            (db_id,),
        ))

    def get_disabled_files(self) -> set[str]:
        try:
            value = json.loads(self.get_meta("disabled_files", "[]"))
            if isinstance(value, list):
                return set(value)
        except (TypeError, ValueError):
            pass
        return set()

    def set_disabled_files(self, disabled: set[str]) -> None:
        self.set_meta("disabled_files", json.dumps(sorted(disabled)))

    # --------------------------------------------------------- Spotify account
    def upsert_spotify_account(
        self,
        account: Mapping[str, Any] | Any | None = None,
        **updates: Any,
    ) -> sqlite3.Row:
        data = _as_mapping(account)
        data.update(updates)
        scopes = _first_value(data, "scopes", "scope", default=[])
        if isinstance(scopes, str):
            scopes = [item for item in scopes.split() if item]
        elif not isinstance(scopes, (list, tuple, set)):
            scopes = []
        now = time.time()
        with self._conn:
            self._conn.execute(
                """INSERT INTO spotify_account
                   (id, spotify_user_id, user_id, display_name, scopes_json, scopes,
                    country, product, href, metadata_json, updated_at)
                   VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     spotify_user_id = COALESCE(NULLIF(excluded.spotify_user_id, ''), spotify_account.spotify_user_id),
                     user_id = COALESCE(NULLIF(excluded.user_id, ''), spotify_account.user_id),
                     display_name = COALESCE(NULLIF(excluded.display_name, ''), spotify_account.display_name),
                     scopes_json = excluded.scopes_json,
                     scopes = excluded.scopes,
                     country = COALESCE(NULLIF(excluded.country, ''), spotify_account.country),
                     product = COALESCE(NULLIF(excluded.product, ''), spotify_account.product),
                     href = COALESCE(NULLIF(excluded.href, ''), spotify_account.href),
                     metadata_json = excluded.metadata_json,
                     updated_at = excluded.updated_at""",
                (
                    str(_first_value(data, "spotify_user_id", "user_id", "id", default="") or ""),
                    str(_first_value(data, "user_id", "spotify_user_id", "id", default="") or ""),
                    str(_first_value(data, "display_name", "name", default="") or ""),
                    _json_text(list(scopes), []),
                    _json_text(list(scopes), []),
                    str(_first_value(data, "country", default="") or ""),
                    str(_first_value(data, "product", default="") or ""),
                    str(_first_value(data, "href", "url", default="") or ""),
                    _json_text(_first_value(data, "metadata", "metadata_json", default={}), {}),
                    now,
                ),
            )
        return self._conn.execute("SELECT * FROM spotify_account WHERE id = 1").fetchone()

    save_spotify_account = upsert_spotify_account
    upsert_account = upsert_spotify_account

    def get_spotify_account(self) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM spotify_account WHERE id = 1").fetchone()

    def clear_spotify_account(self) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM spotify_account")

    # ------------------------------------------------------------ Spotify data
    def upsert_spotify_playlist(self, playlist: Mapping[str, Any] | Any) -> int:
        data = _normalise_spotify_playlist(playlist)
        if not data["spotify_id"]:
            raise ValueError("Spotify playlist requires an id")
        with self._conn:
            self._conn.execute(
                """INSERT INTO spotify_playlists
                   (spotify_id, playlist_id, name, url, owner_id, owner_name,
                    is_owned, collaborative, public, access_status, access_reason,
                    snapshot_id, item_count, artwork_url, artwork_id,
                    metadata_json, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(spotify_id) DO UPDATE SET
                     playlist_id = excluded.playlist_id,
                     name = COALESCE(NULLIF(excluded.name, ''), spotify_playlists.name),
                     url = COALESCE(NULLIF(excluded.url, ''), spotify_playlists.url),
                     owner_id = COALESCE(NULLIF(excluded.owner_id, ''), spotify_playlists.owner_id),
                     owner_name = COALESCE(NULLIF(excluded.owner_name, ''), spotify_playlists.owner_name),
                     is_owned = COALESCE(excluded.is_owned, spotify_playlists.is_owned),
                     collaborative = COALESCE(excluded.collaborative, spotify_playlists.collaborative),
                     public = COALESCE(excluded.public, spotify_playlists.public),
                     access_status = excluded.access_status,
                     access_reason = excluded.access_reason,
                     snapshot_id = COALESCE(NULLIF(excluded.snapshot_id, ''), spotify_playlists.snapshot_id),
                     item_count = COALESCE(excluded.item_count, spotify_playlists.item_count),
                     artwork_url = excluded.artwork_url,
                     artwork_id = COALESCE(NULLIF(excluded.artwork_id, ''), spotify_playlists.artwork_id),
                     metadata_json = excluded.metadata_json,
                     updated_at = excluded.updated_at""",
                (
                    data["spotify_id"],
                    data["playlist_id"],
                    data["name"],
                    data["url"],
                    data["owner_id"],
                    data["owner_name"],
                    None if data["is_owned"] is None else int(bool(data["is_owned"])),
                    None if data["collaborative"] is None else int(bool(data["collaborative"])),
                    None if data["public"] is None else int(bool(data["public"])),
                    data["access_status"],
                    data["access_reason"],
                    data["snapshot_id"],
                    data["item_count"],
                    data["artwork_url"],
                    data["artwork_id"],
                    _json_text(data["metadata"], {}),
                    time.time(),
                ),
            )
            row = self._conn.execute(
                "SELECT id FROM spotify_playlists WHERE spotify_id = ?",
                (data["spotify_id"],),
            ).fetchone()
        return int(row["id"])

    def upsert_spotify_playlists(self, playlists: Iterable[Any]) -> list[int]:
        ids: list[int] = []
        with self._conn:
            for playlist in playlists:
                data = _normalise_spotify_playlist(playlist)
                if data["spotify_id"]:
                    ids.append(self.upsert_spotify_playlist(data))
        return ids

    save_spotify_playlist = upsert_spotify_playlist
    save_spotify_playlists = upsert_spotify_playlists

    def get_spotify_playlists(self, include_inaccessible: bool = True) -> list[sqlite3.Row]:
        sql = "SELECT * FROM spotify_playlists"
        if not include_inaccessible:
            sql += " WHERE access_status = 'accessible'"
        sql += " ORDER BY name COLLATE NOCASE, id"
        return list(self._conn.execute(sql))

    def get_spotify_playlist(self, playlist_id: str | int) -> sqlite3.Row | None:
        if isinstance(playlist_id, int):
            return self._conn.execute(
                "SELECT * FROM spotify_playlists WHERE id = ?", (playlist_id,)
            ).fetchone()
        text = str(playlist_id)
        return self._conn.execute(
            "SELECT * FROM spotify_playlists WHERE spotify_id = ? OR playlist_id = ? OR id = ?",
            (text, text, int(text) if text.isdigit() else -1),
        ).fetchone()

    get_playlists = get_spotify_playlists
    get_playlist = get_spotify_playlist

    def _track_db_id(self, value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, int) and not isinstance(value, bool):
            row = self._conn.execute("SELECT id FROM spotify_tracks WHERE id = ?", (value,)).fetchone()
            return int(row["id"]) if row else None
        text = str(value)
        row = self._conn.execute(
            "SELECT id FROM spotify_tracks WHERE spotify_id = ? OR track_key = ? OR id = ?",
            (text, text, int(text) if text.isdigit() else -1),
        ).fetchone()
        return int(row["id"]) if row else None

    def _upsert_spotify_track(self, value: Any) -> tuple[Track, list[dict[str, Any]], bool, int]:
        if isinstance(value, str) and value.strip():
            existing_id = self._track_db_id(value.strip())
            if existing_id is not None:
                existing_track = next(
                    (item for item in self.get_spotify_tracks(True) if item.db_id == existing_id),
                    None,
                )
                if existing_track is not None:
                    return existing_track, [], False, existing_id
            value = {"spotify_id": value.strip()}
        track, playlists, liked, liked_added_at = _normalise_spotify_track(value)
        existing = self._conn.execute(
            "SELECT id, origin_json FROM spotify_tracks WHERE spotify_id = ? LIMIT 1",
            (track.spotify_id,),
        ).fetchone() if track.spotify_id else None
        if existing is None:
            existing = self._conn.execute(
                "SELECT id, origin_json FROM spotify_tracks WHERE track_key = ? LIMIT 1",
                (track.track_key,),
            ).fetchone()
        origin = _load_json(existing["origin_json"], {}) if existing else {}
        if not isinstance(origin, dict):
            origin = {}
        incoming_origin = dict(track.origin_metadata)
        if isinstance(origin.get("playlist_ids"), list) and isinstance(
            incoming_origin.get("playlist_ids"), list
        ):
            incoming_origin["playlist_ids"] = list(
                dict.fromkeys([*origin["playlist_ids"], *incoming_origin["playlist_ids"]])
            )
        origin.update(incoming_origin)
        if existing:
            track_id = int(existing["id"])
            self._conn.execute(
                """UPDATE spotify_tracks SET
                   spotify_id = COALESCE(NULLIF(?, ''), spotify_id),
                   title = COALESCE(NULLIF(?, ''), title),
                   title_norm = COALESCE(NULLIF(?, ''), title_norm),
                   artist_full = COALESCE(NULLIF(?, ''), artist_full),
                   artist_norm = COALESCE(NULLIF(?, ''), artist_norm),
                   primary_artist = COALESCE(NULLIF(?, ''), primary_artist),
                   primary_artist_norm = COALESCE(NULLIF(?, ''), primary_artist_norm),
                   featured_json = CASE WHEN ? != '[]' THEN ? ELSE featured_json END,
                   album = COALESCE(NULLIF(?, ''), album),
                   album_norm = COALESCE(NULLIF(?, ''), album_norm),
                   album_id = COALESCE(NULLIF(?, ''), album_id),
                   duration_ms = COALESCE(?, duration_ms),
                   isrc = COALESCE(NULLIF(?, ''), isrc),
                   spotify_url = COALESCE(NULLIF(?, ''), spotify_url),
                   origin = COALESCE(NULLIF(?, ''), origin),
                    origin_json = ?, origin_metadata_json = ?,
                   added_at = COALESCE(NULLIF(?, ''), added_at),
                   spotify_synced_at = ?
                   WHERE id = ?""",
                (
                    track.spotify_id,
                    track.title,
                    track.title_norm,
                    track.artist_full,
                    track.artist_norm,
                    track.primary_artist,
                    track.primary_artist_norm,
                    _json_text(track.featured_artists, []),
                    _json_text(track.featured_artists, []),
                    track.album,
                    track.album_norm,
                    track.album_id,
                    track.duration_ms,
                    track.isrc,
                    track.spotify_url,
                    track.origin,
                    _json_text(origin, {}),
                    _json_text(origin, {}),
                    track.added_at or "",
                    time.time(),
                    track_id,
                ),
            )
        else:
            cur = self._conn.execute(
                """INSERT INTO spotify_tracks
                   (track_key, spotify_id, title, title_norm, artist_full, artist_norm,
                    primary_artist, primary_artist_norm, featured_json, album, album_norm,
                    album_id, duration_ms, isrc, spotify_url, origin, origin_json,
                    origin_metadata_json, added_at, spotify_synced_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    track.track_key,
                    track.spotify_id,
                    track.title,
                    track.title_norm,
                    track.artist_full,
                    track.artist_norm,
                    track.primary_artist,
                    track.primary_artist_norm,
                    _json_text(track.featured_artists, []),
                    track.album,
                    track.album_norm,
                    track.album_id,
                    track.duration_ms,
                    track.isrc,
                    track.spotify_url,
                    track.origin,
                    _json_text(origin, {}),
                    _json_text(origin, {}),
                    track.added_at,
                    time.time(),
                ),
            )
            track_id = int(cur.lastrowid)
        track.db_id = track_id
        if liked:
            self._mark_spotify_liked(track_id, track.spotify_id, liked_added_at)
        for playlist in playlists:
            self._link_spotify_playlist_track(track_id, track.spotify_id, playlist)
        return track, playlists, liked, track_id

    def _mark_spotify_liked(
        self,
        track_id: int,
        spotify_track_id: str,
        added_at: str | None = None,
    ) -> None:
        if not spotify_track_id:
            return
        self._conn.execute(
            """INSERT INTO spotify_liked_tracks
               (track_id, spotify_track_id, added_at, status, metadata_json)
               VALUES (?, ?, ?, 'imported', '{}')
               ON CONFLICT(track_id) DO UPDATE SET
                 spotify_track_id = excluded.spotify_track_id,
                 added_at = COALESCE(excluded.added_at, spotify_liked_tracks.added_at),
                 status = 'imported'""",
            (track_id, spotify_track_id, added_at),
        )

    def upsert_spotify_tracks(
        self,
        tracks: Iterable[Any] | Any,
        memberships: Any = None,
        liked_tracks: Iterable[Any] | None = None,
        *,
        playlists: Iterable[Any] | None = None,
    ) -> list[Track]:
        """Upsert deduplicated Spotify records and all record memberships."""
        data = _as_mapping(tracks)
        if data and "records" in data:
            tracks = data.get("records") or data.get("tracks") or []
        elif hasattr(tracks, "records"):
            tracks = getattr(tracks, "records")
        elif data and (
            "spotify_id" in data or "track_id" in data or "title" in data or "name" in data
        ):
            tracks = [tracks]
        if isinstance(tracks, Mapping) or isinstance(tracks, (str, bytes)):
            tracks = [tracks]
        result: list[Track] = []
        with self._conn:
            for value in tracks or []:
                track, _, _, _ = self._upsert_spotify_track(value)
                result.append(track)
            if playlists:
                self.upsert_spotify_playlists(playlists)
            if memberships is not None:
                self._upsert_spotify_memberships(memberships)
            for value in liked_tracks or []:
                track, _, _, track_id = self._upsert_spotify_track(value)
                self._mark_spotify_liked(track_id, track.spotify_id, track.added_at)
        return result

    save_spotify_tracks = upsert_spotify_tracks

    def upsert_spotify_track(self, track: Any) -> Track:
        tracks = self.upsert_spotify_tracks([track])
        if not tracks:
            raise ValueError("Spotify track is required")
        return tracks[0]

    save_spotify_track = upsert_spotify_track

    def upsert_spotify_memberships(self, memberships: Any) -> int:
        with self._conn:
            return self._upsert_spotify_memberships(memberships)

    def _upsert_spotify_memberships(self, memberships: Any) -> int:
        """Persist playlist memberships in common mapping/tuple payload shapes."""
        count = 0
        if isinstance(memberships, Mapping):
            batches = memberships.items()
            for playlist_value, values in batches:
                playlist = playlist_value
                if isinstance(playlist_value, str):
                    playlist = {"id": playlist_value, "name": playlist_value}
                for position, value in enumerate(values or []):
                    track, _, _, track_id = self._upsert_spotify_track(value)
                    self._link_spotify_playlist_track(
                        track_id,
                        track.spotify_id,
                        playlist,
                        position=position,
                    )
                    count += 1
            return count
        for batch in memberships or []:
            if isinstance(batch, Mapping) and ("items" in batch or "tracks" in batch):
                playlist = batch.get("playlist") or batch.get("playlist_id") or batch.get("id")
                values = batch.get("items", batch.get("tracks", []))
                if isinstance(playlist, str):
                    playlist = {"id": playlist, "name": playlist}
                for position, value in enumerate(values or []):
                    track, _, _, track_id = self._upsert_spotify_track(value)
                    self._link_spotify_playlist_track(track_id, track.spotify_id, playlist, position=position)
                    count += 1
                continue
            try:
                playlist, value = batch
            except (TypeError, ValueError):
                continue
            track, _, _, track_id = self._upsert_spotify_track(value)
            self._link_spotify_playlist_track(track_id, track.spotify_id, playlist)
            count += 1
        return count

    upsert_playlist_memberships = upsert_spotify_memberships
    record_spotify_memberships = upsert_spotify_memberships

    def _link_spotify_playlist_track(
        self,
        track_id: int,
        spotify_track_id: str,
        playlist: Mapping[str, Any] | Any,
        *,
        position: int | None = None,
        added_at: str | None = None,
        status: str = "imported",
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        data = _normalise_spotify_playlist(playlist)
        if not data["spotify_id"]:
            return
        playlist_db_id = self.upsert_spotify_playlist(data)
        self._conn.execute(
            """INSERT INTO spotify_playlist_tracks
               (playlist_id, spotify_playlist_id, track_id, spotify_track_id,
                position, added_at, status, metadata_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(playlist_id, spotify_track_id) DO UPDATE SET
                 track_id = excluded.track_id,
                 position = COALESCE(excluded.position, spotify_playlist_tracks.position),
                 added_at = COALESCE(excluded.added_at, spotify_playlist_tracks.added_at),
                 status = excluded.status,
                 metadata_json = excluded.metadata_json""",
            (
                playlist_db_id,
                data["spotify_id"],
                track_id,
                spotify_track_id,
                position,
                added_at,
                status,
                _json_text(metadata or {}, {}),
            ),
        )

    def replace_spotify_playlist_memberships(
        self,
        playlist: Mapping[str, Any] | Any,
        tracks: Iterable[Any],
    ) -> None:
        data = _normalise_spotify_playlist(playlist)
        if not data["spotify_id"]:
            raise ValueError("Spotify playlist requires an id")
        playlist_db_id = self.upsert_spotify_playlist(data)
        with self._conn:
            self._conn.execute(
                "DELETE FROM spotify_playlist_tracks WHERE playlist_id = ?",
                (playlist_db_id,),
            )
            for position, value in enumerate(tracks):
                track, _, _, track_id = self._upsert_spotify_track(value)
                item_data = _as_mapping(value)
                if isinstance(item_data.get("track"), Mapping):
                    item_data = dict(item_data.get("track", {})) | {
                        key: item_data[key]
                        for key in ("added_at", "position")
                        if key in item_data
                    }
                self._link_spotify_playlist_track(
                    track_id,
                    track.spotify_id,
                    data,
                    position=item_data.get("position", position),
                    added_at=item_data.get("added_at"),
                )

    replace_playlist_memberships = replace_spotify_playlist_memberships

    def replace_spotify_library(
        self,
        result: Any = None,
        *,
        records: Iterable[Any] | None = None,
        playlists: Iterable[Any] | None = None,
        liked_tracks: Iterable[Any] | None = None,
        outcomes: Iterable[Any] | None = None,
        workspace_id: int | str | None = None,
    ) -> list[Track]:
        """Persist a ``spotify.parse_library`` result without a Spotify import."""
        result_data = _as_mapping(result)
        if records is None:
            if "records" in result_data:
                records = result_data.get("records")
            elif "tracks" in result_data:
                records = result_data.get("tracks")
        if (
            records is None
            and result is not None
            and not result_data
            and isinstance(result, Iterable)
            and not isinstance(result, (str, bytes, Mapping))
        ):
            records = result
        if records is None and result is not None and hasattr(result, "records"):
            records = getattr(result, "records")
        if playlists is None:
            playlists = result_data.get("playlists")
        if playlists is None and result is not None and hasattr(result, "playlists"):
            playlists = getattr(result, "playlists")
        if outcomes is None:
            outcomes = result_data.get("outcomes")
        if outcomes is None and result is not None and hasattr(result, "outcomes"):
            outcomes = getattr(result, "outcomes")
        with self._conn:
            # A library result is a complete snapshot.  Remove only Spotify
            # membership links; legacy CSV files, tracks, candidates, and
            # download state remain untouched.
            if records is not None or liked_tracks is not None:
                self._conn.execute("DELETE FROM spotify_playlist_tracks")
                self._conn.execute("DELETE FROM spotify_liked_tracks")
            if playlists:
                self.upsert_spotify_playlists(playlists)
            stored = self.upsert_spotify_tracks(records or [])
            if liked_tracks:
                for value in liked_tracks:
                    track, _, _, track_id = self._upsert_spotify_track(value)
                    if track.spotify_id:
                        self._conn.execute(
                            """INSERT INTO spotify_liked_tracks
                               (track_id, spotify_track_id, added_at, status, metadata_json)
                               VALUES (?, ?, ?, 'imported', '{}')
                               ON CONFLICT(track_id) DO UPDATE SET status = 'imported'""",
                            (track_id, track.spotify_id, track.added_at),
                        )
            for outcome in outcomes or []:
                self.record_spotify_item_status(outcome, workspace_id=workspace_id)
            if workspace_id is not None:
                self.replace_workspace_library_tracks(workspace_id, stored)
                scope = self.get_workspace_scope_data(workspace_id)
                if scope is not None:
                    self.set_workspace_scope(
                        workspace_id,
                        scope.get("playlist_ids", []),
                        bool(scope.get("include_liked")),
                        [track.spotify_id for track in stored if track.spotify_id],
                    )
        return stored

    upsert_spotify_library = replace_spotify_library
    sync_spotify_library = replace_spotify_library
    persist_spotify_library = replace_spotify_library
    save_spotify_library = replace_spotify_library

    # ------------------------------------------------------ scoped workspaces
    def set_workspace_scope(
        self,
        workspace_id: int | str,
        playlist_ids: Iterable[Any] | None = None,
        include_liked: bool = False,
        track_ids: Iterable[Any] | None = None,
    ) -> sqlite3.Row:
        db_id = self._workspace_db_id(workspace_id)
        playlists = sorted({str(value).strip() for value in (playlist_ids or ()) if str(value).strip()})
        tracks = sorted({str(value).strip() for value in (track_ids or ()) if str(value).strip()})
        payload = {"playlist_ids": playlists, "include_liked": bool(include_liked), "track_ids": tracks}
        fingerprint = hashlib.sha256(_json_text(payload, {}).encode("utf-8")).hexdigest()
        with self._conn:
            self._conn.execute(
                """INSERT INTO workspace_scope
                   (workspace_id, playlist_ids_json, include_liked, track_ids_json, fingerprint, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(workspace_id) DO UPDATE SET
                     playlist_ids_json = excluded.playlist_ids_json,
                     include_liked = excluded.include_liked,
                     track_ids_json = excluded.track_ids_json,
                     fingerprint = excluded.fingerprint,
                     updated_at = excluded.updated_at""",
                (db_id, _json_text(playlists, []), int(bool(include_liked)), _json_text(tracks, []), fingerprint, time.time()),
            )
        row = self._conn.execute("SELECT * FROM workspace_scope WHERE workspace_id = ?", (db_id,)).fetchone()
        assert row is not None
        return row

    save_workspace_scope = set_workspace_scope

    def get_workspace_minimum_score(self, workspace_id: int | str) -> float:
        """Return the persisted public match threshold, defaulting to 50."""
        row = self.get_workspace(workspace_id)
        if row is None:
            raise ValueError(f"unknown discovery workspace: {workspace_id}")
        metadata = _load_json(row["metadata_json"], {})
        try:
            value = float(metadata.get("minimum_score", 50)) if isinstance(metadata, dict) else 50.0
        except (TypeError, ValueError):
            value = 50.0
        return max(0.0, min(100.0, value))

    def set_workspace_minimum_score(self, workspace_id: int | str, minimum_score: Any) -> float:
        try:
            value = float(minimum_score)
        except (TypeError, ValueError) as exc:
            raise ValueError("minimum_score must be a number from 0 to 100") from exc
        if not 0 <= value <= 100:
            raise ValueError("minimum_score must be a number from 0 to 100")
        row = self.get_workspace(workspace_id)
        if row is None:
            raise ValueError(f"unknown discovery workspace: {workspace_id}")
        metadata = _load_json(row["metadata_json"], {})
        if not isinstance(metadata, dict):
            metadata = {}
        metadata["minimum_score"] = value
        self.update_workspace(workspace_id, metadata=metadata)
        return value

    def get_workspace_scope(self, workspace_id: int | str) -> sqlite3.Row | None:
        db_id = self._workspace_db_id(workspace_id)
        return self._conn.execute("SELECT * FROM workspace_scope WHERE workspace_id = ?", (db_id,)).fetchone()

    def get_workspace_scope_data(self, workspace_id: int | str) -> dict[str, Any] | None:
        row = self.get_workspace_scope(workspace_id)
        if row is None:
            return None
        return {
            "workspace_id": int(row["workspace_id"]),
            "playlist_ids": _load_json(row["playlist_ids_json"], []),
            "include_liked": bool(row["include_liked"]),
            "track_ids": _load_json(row["track_ids_json"], []),
            "fingerprint": row["fingerprint"] or "",
            "updated_at": row["updated_at"],
        }

    def replace_workspace_library_tracks(
        self, workspace_id: int | str, tracks: Iterable[Track]
    ) -> int:
        db_id = self._workspace_db_id(workspace_id)
        values = list(tracks)
        with self._conn:
            self._conn.execute("DELETE FROM workspace_library_tracks WHERE workspace_id = ?", (db_id,))
            for track in values:
                if track.db_id is None:
                    continue
                owned = self.get_inventory_identity_matches(track.primary_artist_norm, track.title_norm)
                membership = {
                    "playlists": list(track.playlists),
                    "spotify_id": track.spotify_id,
                }
                self._conn.execute(
                    """INSERT INTO workspace_library_tracks
                       (workspace_id, track_id, already_owned, owned_count, membership_json)
                       VALUES (?, ?, ?, ?, ?)""",
                    (db_id, int(track.db_id), int(len(owned) == 1), len(owned), _json_text(membership, {})),
                )
        return len(values)

    def get_workspace_library_track_ids(self, workspace_id: int | str) -> set[int]:
        db_id = self._workspace_db_id(workspace_id)
        return {int(row["track_id"]) for row in self._conn.execute(
            "SELECT track_id FROM workspace_library_tracks WHERE workspace_id = ?", (db_id,)
        )}

    def get_workspace_owned_track_ids(self, workspace_id: int | str) -> set[int]:
        db_id = self._workspace_db_id(workspace_id)
        return {int(row["track_id"]) for row in self._conn.execute(
            "SELECT track_id FROM workspace_library_tracks WHERE workspace_id = ? AND already_owned = 1", (db_id,)
        )}

    def recover_interrupted_discovery(self) -> int:
        """Normalize daemon jobs left running when the local app stopped."""
        recovered = 0
        with self._conn:
            rows = list(self._conn.execute(
                "SELECT id, metadata_json FROM discovery_workspaces WHERE status IN ('running', 'pending')"
            ))
            for row in rows:
                metadata = _load_json(row["metadata_json"], {})
                if not isinstance(metadata, dict):
                    metadata = {}
                metadata.update({"interrupted": True, "resume_message": "The service stopped; cached searches are resumable."})
                self._conn.execute(
                    "UPDATE discovery_workspaces SET status = 'interrupted', phase = 'interrupted', metadata_json = ?, updated_at = ? WHERE id = ?",
                    (_json_text(metadata, {}), time.time(), row["id"]),
                )
                self._conn.execute(
                    "UPDATE discovery_progress SET status = 'interrupted', phase = 'interrupted', message = ?, updated_at = ? WHERE workspace_id = ?",
                    (metadata["resume_message"], time.time(), row["id"]),
                )
                self._conn.execute(
                    "UPDATE discovery_tasks SET status = 'interrupted', error = ?, finished_at = ? WHERE workspace_id = ? AND status IN ('running', 'pending')",
                    (metadata["resume_message"], time.time(), row["id"]),
                )
                self._conn.execute(
                    "UPDATE discovery_scans SET status = 'interrupted', finished_at = ? WHERE workspace_id = ? AND status = 'running'",
                    (time.time(), row["id"]),
                )
                recovered += 1
        return recovered

    def recover_discovery_run(self, workspace_id: int | str, run_id: str) -> bool:
        """Mark one still-active legacy run interrupted, without touching newer runs."""
        db_id = self._workspace_db_id(workspace_id)
        now = time.time()
        message = "The local Spotify worker stopped; cached work can be resumed."
        with self._conn:
            row = self._conn.execute(
                "SELECT id, status, metadata_json FROM discovery_workspaces WHERE id = ?",
                (db_id,),
            ).fetchone()
            if row is None or str(row["status"]) not in {"running", "pending"}:
                return False
            metadata = _load_json(row["metadata_json"], {})
            if not isinstance(metadata, dict) or str(metadata.get("run_id") or "") != str(run_id):
                return False
            metadata.update({"interrupted": True, "resume_message": message})
            self._conn.execute(
                "UPDATE discovery_workspaces SET status = 'interrupted', phase = 'interrupted', metadata_json = ?, completed_at = ?, updated_at = ? WHERE id = ? AND status IN ('running', 'pending')",
                (_json_text(metadata, {}), now, now, db_id),
            )
            self._conn.execute(
                "UPDATE discovery_progress SET status = 'interrupted', phase = 'interrupted', message = ?, error = ?, updated_at = ? WHERE workspace_id = ?",
                (message, message, now, db_id),
            )
            self._conn.execute(
                "UPDATE discovery_tasks SET status = 'interrupted', error = ?, finished_at = ? WHERE workspace_id = ? AND status IN ('running', 'pending')",
                (message, now, db_id),
            )
            self._conn.execute(
                "UPDATE discovery_scans SET status = 'interrupted', finished_at = ? WHERE workspace_id = ? AND status = 'running'",
                (now, db_id),
            )
            self._conn.execute(
                "UPDATE workspace_expansion_batches SET status = 'interrupted', phase = 'interrupted', error = ?, completed_at = ?, heartbeat_at = ?, updated_at = ? WHERE workspace_id = ? AND run_id = ? AND status IN ('running', 'pending')",
                (message, now, now, now, db_id, str(run_id)),
            )
        return True

    # -------------------------------------------------------- local inventory
    def get_inventory_root(self, root_path: str | Path) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM inventory_roots WHERE root_path = ?", (str(Path(root_path)),)).fetchone()

    def get_inventory_entries(self, root_path: str | Path) -> list[sqlite3.Row]:
        root = self.get_inventory_root(root_path)
        if root is None:
            return []
        return list(self._conn.execute("SELECT * FROM inventory_charts WHERE root_id = ? ORDER BY relative_path", (root["id"],)))

    def save_inventory_result(self, result: Any) -> sqlite3.Row:
        root_path = str(Path(result.root))
        entries = list(getattr(result, "entries", ()) or ())
        progress = getattr(result, "progress", None)
        now = time.time()
        with self._conn:
            self._conn.execute(
                """INSERT INTO inventory_roots (root_path, status, scanned_at, file_count, valid_count, invalid_count, diagnostic_count, metadata_json, updated_at)
                   VALUES (?, 'complete', ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(root_path) DO UPDATE SET status='complete', scanned_at=excluded.scanned_at,
                     file_count=excluded.file_count, valid_count=excluded.valid_count, invalid_count=excluded.invalid_count,
                     diagnostic_count=excluded.diagnostic_count, metadata_json=excluded.metadata_json, updated_at=excluded.updated_at""",
                (root_path, now, len(entries), len(getattr(result, "valid_entries", []) or []), len(getattr(result, "invalid_entries", []) or []),
                 len(getattr(result, "diagnostics", ()) or ()), _json_text({"diagnostics": list(getattr(result, "diagnostics", ()) or ())}, {}), now),
            )
            root = self._conn.execute("SELECT id FROM inventory_roots WHERE root_path = ?", (root_path,)).fetchone()
            assert root is not None
            root_id = int(root["id"])
            seen: list[str] = []
            for entry in entries:
                relative = str(entry.relative_path).replace("\\", "/")
                seen.append(relative)
                validation = getattr(entry, "validation", None)
                state = "valid" if getattr(validation, "valid", False) else "invalid"
                self._conn.execute(
                    """INSERT INTO inventory_charts
                       (root_id, relative_path, size_bytes, mtime_ns, sha256, artist, title, album, duration_ms,
                        artist_norm, title_norm, validation_state, parser_source, diagnostics_json, scanned_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(root_id, relative_path) DO UPDATE SET size_bytes=excluded.size_bytes,
                        mtime_ns=excluded.mtime_ns, sha256=excluded.sha256, artist=excluded.artist, title=excluded.title,
                        album=excluded.album, duration_ms=excluded.duration_ms, artist_norm=excluded.artist_norm,
                        title_norm=excluded.title_norm, validation_state=excluded.validation_state,
                        parser_source=excluded.parser_source, diagnostics_json=excluded.diagnostics_json, scanned_at=excluded.scanned_at""",
                    (root_id, relative, int(getattr(entry, "size", 0) or 0), int(getattr(entry, "mtime_ns", 0) or 0), getattr(entry, "sha256", None),
                     str(getattr(entry, "artist", "") or ""), str(getattr(entry, "title", "") or ""), str(getattr(entry, "album", "") or ""),
                     getattr(entry, "duration_ms", None), str(getattr(entry, "artist_norm", "") or ""), str(getattr(entry, "title_norm", "") or ""),
                     state, str(getattr(entry, "parser_source", "") or ""), _json_text(list(getattr(entry, "diagnostics", ()) or ()), []), now),
                )
            if seen:
                placeholders = ",".join("?" for _ in seen)
                self._conn.execute("DELETE FROM inventory_charts WHERE root_id = ? AND relative_path NOT IN (" + placeholders + ")", [root_id, *seen])
            else:
                self._conn.execute("DELETE FROM inventory_charts WHERE root_id = ?", (root_id,))
        return self._conn.execute("SELECT * FROM inventory_roots WHERE id = ?", (root_id,)).fetchone()

    def get_inventory_identity_matches(self, artist_norm: str, title_norm: str) -> list[sqlite3.Row]:
        if not artist_norm or not title_norm:
            return []
        return list(self._conn.execute(
            """SELECT c.*, r.root_path FROM inventory_charts c JOIN inventory_roots r ON r.id = c.root_id
               WHERE c.validation_state = 'valid' AND c.artist_norm = ? AND c.title_norm = ?
               ORDER BY c.relative_path""", (artist_norm, title_norm)
        ))

    inventory_identity_matches = get_inventory_identity_matches

    def remove_inventory_entries(self, root_path: str | Path, relative_paths: Iterable[str]) -> int:
        """Drop inventory rows (e.g. quarantined files) and refresh root counts."""
        root = self.get_inventory_root(root_path)
        if root is None:
            return 0
        removed = 0
        with self._conn:
            for relative in relative_paths:
                cursor = self._conn.execute(
                    "DELETE FROM inventory_charts WHERE root_id = ? AND relative_path = ?",
                    (root["id"], str(relative)),
                )
                removed += int(cursor.rowcount or 0)
            if removed:
                self._conn.execute(
                    """UPDATE inventory_roots SET
                         file_count = (SELECT COUNT(*) FROM inventory_charts c WHERE c.root_id = ?),
                         valid_count = (SELECT COUNT(*) FROM inventory_charts c WHERE c.root_id = ? AND c.validation_state = 'valid'),
                         invalid_count = (SELECT COUNT(*) FROM inventory_charts c WHERE c.root_id = ? AND c.validation_state != 'valid'),
                         updated_at = ?
                       WHERE id = ?""",
                    (root["id"], root["id"], root["id"], time.time(), root["id"]),
                )
        return removed

    def replace_spotify_tracks(
        self,
        tracks: Iterable[Any] | Any,
        memberships: Any = None,
        playlists: Iterable[Any] | None = None,
        liked_tracks: Iterable[Any] | None = None,
    ) -> list[Track]:
        """Replace the current Spotify membership snapshot without touching CSV rows."""
        records_data = _as_mapping(tracks)
        if records_data and "records" in records_data:
            tracks = records_data["records"]
        elif hasattr(tracks, "records"):
            tracks = getattr(tracks, "records")
        if isinstance(tracks, Iterable) and not isinstance(tracks, (str, bytes, Mapping)):
            values_list = list(tracks)
        else:
            values_list = [tracks]
        incoming_ids: set[str] = set()
        for value in values_list:
            data = _as_mapping(value)
            if isinstance(data.get("track"), Mapping):
                data = data["track"]
            track_id = str(_first_value(data, "spotify_id", "track_id", "id", default="") or "")
            if track_id:
                incoming_ids.add(track_id)
        with self._conn:
            if incoming_ids:
                placeholders = ",".join("?" for _ in incoming_ids)
                self._conn.execute(
                    "DELETE FROM spotify_playlist_tracks WHERE spotify_track_id NOT IN (" + placeholders + ")",
                    list(incoming_ids),
                )
                self._conn.execute(
                    "DELETE FROM spotify_liked_tracks WHERE spotify_track_id NOT IN (" + placeholders + ")",
                    list(incoming_ids),
                )
            else:
                self._conn.execute("DELETE FROM spotify_playlist_tracks")
                self._conn.execute("DELETE FROM spotify_liked_tracks")
        with self._conn:
            stored = self.upsert_spotify_tracks(values_list)
            if playlists:
                self.upsert_spotify_playlists(playlists)
            if memberships is not None:
                self._upsert_spotify_memberships(memberships)
            if liked_tracks:
                for item in liked_tracks:
                    track, _, _, track_id = self._upsert_spotify_track(item)
                    self._mark_spotify_liked(track_id, track.spotify_id, track.added_at)
        return stored

    # ------------------------------------------------------- item terminal state
    def record_spotify_item_status(
        self,
        item: Mapping[str, Any] | Any | None = None,
        *,
        workspace_id: int | str | None = None,
        source: str | None = None,
        container_id: str = "",
        item_key: str = "",
        spotify_id: str = "",
        title: str = "",
        status: str | Any = "",
        reason: str = "",
        position: int | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> int:
        data = _as_mapping(item)
        source = source or str(_first_value(data, "source", "kind", default="") or "")
        raw_status = status or _first_value(data, "status", "terminal_status", default="error")
        raw_status = getattr(raw_status, "value", raw_status)
        status = str(raw_status or "error")
        spotify_id = spotify_id or str(
            _first_value(data, "spotify_id", "track_id", "id", default="") or ""
        )
        title = title or str(_first_value(data, "title", "name", default="") or "")
        playlist_data = data.get("playlist")
        if isinstance(playlist_data, Mapping):
            playlist_id_from_nested = _first_value(
                playlist_data, "spotify_id", "playlist_id", "id", default=""
            )
        else:
            playlist_id_from_nested = ""
        playlist_id = str(
            _first_value(
                data,
                "playlist_id",
                "container_id",
                default=playlist_id_from_nested,
            )
            or ""
        )
        container_id = container_id or playlist_id
        if not source:
            source = "liked" if not container_id else "playlist"
        item_key = item_key or str(
            _first_value(
                data,
                "item_key",
                default=spotify_id or title or position or reason or "item",
            )
            or "item"
        )
        reason = reason or str(_first_value(data, "reason", default="") or "")
        if position is None:
            raw_position = _first_value(data, "position", default=None)
            try:
                position = int(raw_position) if raw_position is not None else None
            except (TypeError, ValueError):
                position = None
        detail_value = detail or _first_value(data, "detail", "metadata", default={})
        workspace_db_id = self._workspace_db_id(workspace_id) if workspace_id is not None else None
        now = time.time()
        with self._conn:
            # SQLite treats NULLs as distinct in UNIQUE constraints.  Delete
            # the old global row explicitly so non-workspace syncs are idempotent.
            if workspace_db_id is None:
                self._conn.execute(
                    "DELETE FROM spotify_item_statuses WHERE workspace_id IS NULL "
                    "AND source = ? AND container_id = ? AND item_key = ?",
                    (source, container_id, item_key),
                )
            else:
                self._conn.execute(
                    "DELETE FROM spotify_item_statuses WHERE workspace_id = ? "
                    "AND source = ? AND container_id = ? AND item_key = ?",
                    (workspace_db_id, source, container_id, item_key),
                )
            cur = self._conn.execute(
                """INSERT INTO spotify_item_statuses
                   (workspace_id, source, container_id, item_key, spotify_id,
                    title, status, reason, position, detail_json, occurred_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    workspace_db_id,
                    source,
                    container_id,
                    item_key,
                    spotify_id,
                    title,
                    status,
                    reason,
                    position,
                    _json_text(detail_value, {}),
                    now,
                    now,
                ),
            )
        return int(cur.lastrowid)

    set_spotify_item_status = record_spotify_item_status

    def record_item_status(self, item: Any = None, *args: Any, **values: Any) -> int:
        if args:
            if len(args) >= 3:
                # (workspace_id, source, item_key, status[, reason])
                workspace_id = item
                values.update(
                    source=args[0],
                    item_key=args[1],
                    status=args[2],
                )
                if len(args) >= 4:
                    values.setdefault("reason", args[3])
                return self.record_spotify_item_status(
                    None, workspace_id=workspace_id, **values
                )
            # (source, item_key, status[, reason])
            values.update(source=item, item_key=args[0], status=args[1])
            if len(args) >= 3:
                values.setdefault("reason", args[2])
            return self.record_spotify_item_status(None, **values)
        return self.record_spotify_item_status(item, **values)

    record_terminal_item_status = record_item_status
    set_terminal_item_status = record_item_status

    def get_spotify_item_status(
        self,
        source: str,
        item_key: str,
        *,
        container_id: str = "",
        workspace_id: int | str | None = None,
    ) -> sqlite3.Row | None:
        workspace_db_id = self._workspace_db_id(workspace_id) if workspace_id is not None else None
        if workspace_db_id is None:
            return self._conn.execute(
                """SELECT * FROM spotify_item_statuses
                   WHERE workspace_id IS NULL AND source = ? AND container_id = ? AND item_key = ?""",
                (source, container_id, item_key),
            ).fetchone()
        return self._conn.execute(
            """SELECT * FROM spotify_item_statuses
               WHERE workspace_id = ? AND source = ? AND container_id = ? AND item_key = ?""",
            (workspace_db_id, source, container_id, item_key),
        ).fetchone()

    def get_spotify_item_statuses(
        self,
        workspace_id: int | str | None = None,
        *,
        source: str | None = None,
        status: str | None = None,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[Any] = []
        if workspace_id is None:
            clauses.append("workspace_id IS NULL")
        else:
            clauses.append("workspace_id = ?")
            params.append(self._workspace_db_id(workspace_id))
        if source:
            clauses.append("source = ?")
            params.append(source)
        if status:
            clauses.append("status = ?")
            params.append(status)
        return list(
            self._conn.execute(
                "SELECT * FROM spotify_item_statuses WHERE "
                + " AND ".join(clauses)
                + " ORDER BY occurred_at, id",
                params,
            )
        )

    def get_terminal_item_status(self, *args: Any, **values: Any) -> sqlite3.Row | None:
        if len(args) >= 3:
            return self.get_spotify_item_status(
                str(args[1]),
                str(args[2]),
                workspace_id=args[0],
                **values,
            )
        return self.get_spotify_item_status(*args, **values)

    get_item_status = get_terminal_item_status
    list_item_statuses = get_spotify_item_statuses

    def get_workspace_item_status(
        self,
        workspace_id: int | str,
        source: str,
        item_key: str,
        *,
        container_id: str = "",
    ) -> sqlite3.Row | None:
        return self.get_spotify_item_status(
            source,
            item_key,
            container_id=container_id,
            workspace_id=workspace_id,
        )

    def record_workspace_item_status(
        self,
        workspace_id: int | str,
        item: Mapping[str, Any] | Any,
        **values: Any,
    ) -> int:
        return self.record_spotify_item_status(item, workspace_id=workspace_id, **values)

    def get_spotify_item_status_counts(
        self,
        workspace_id: int | str | None = None,
    ) -> dict[str, int]:
        return {
            str(row["status"]): int(row["count"])
            for row in self._conn.execute(
                """SELECT status, COUNT(*) AS count FROM spotify_item_statuses
                   WHERE workspace_id IS ? GROUP BY status""",
                (self._workspace_db_id(workspace_id) if workspace_id is not None else None,),
            )
        }

    # -------------------------------------------------------------- workspaces
    def _workspace_db_id(self, value: int | str | Any) -> int:
        if isinstance(value, int) and not isinstance(value, bool):
            row = self._conn.execute(
                "SELECT id FROM discovery_workspaces WHERE id = ?", (value,)
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT id FROM discovery_workspaces WHERE workspace_key = ? OR id = ?",
                (str(value), int(str(value)) if str(value).isdigit() else -1),
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown discovery workspace: {value}")
        return int(row["id"])

    def create_workspace(
        self,
        workspace: Mapping[str, Any] | str | None = None,
        *,
        workspace_key: str = "",
        name: str = "",
        kind: str = "exact",
        status: str = "created",
        phase: str = "idle",
        metadata: Mapping[str, Any] | None = None,
        spotify_account_id: int | None = None,
        **values: Any,
    ) -> int:
        data = _as_mapping(workspace)
        if isinstance(workspace, str):
            data = {"name": workspace, "workspace_key": workspace}
        data.update(values)
        key = str(
            _first_value(
                data,
                "workspace_key",
                "scan_key",
                "key",
                "workspace_id",
                "id",
                default=workspace_key,
            )
            or ""
        )
        if not key:
            key = uuid.uuid4().hex
        actual_name = str(_first_value(data, "name", "label", default=name) or "")
        actual_kind = str(_first_value(data, "kind", "scan_kind", default=kind) or kind)
        actual_status = str(_first_value(data, "status", default=status) or status)
        actual_phase = str(_first_value(data, "phase", default=phase) or phase)
        now = time.time()
        with self._conn:
            existing = self._conn.execute(
                "SELECT id FROM discovery_workspaces WHERE workspace_key = ?", (key,)
            ).fetchone()
            if existing:
                workspace_id = int(existing["id"])
                self._conn.execute(
                    """UPDATE discovery_workspaces SET name = ?, kind = ?, status = ?,
                       phase = ?, metadata_json = ?, updated_at = ? WHERE id = ?""",
                    (
                        actual_name,
                        actual_kind,
                        actual_status,
                        actual_phase,
                        _json_text(_first_value(data, "metadata", default=metadata or {}), {}),
                        now,
                        workspace_id,
                    ),
                )
            else:
                cur = self._conn.execute(
                    """INSERT INTO discovery_workspaces
                       (workspace_key, name, kind, status, phase, spotify_account_id,
                        metadata_json, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        key,
                        actual_name,
                        actual_kind,
                        actual_status,
                        actual_phase,
                        spotify_account_id,
                        _json_text(_first_value(data, "metadata", default=metadata or {}), {}),
                        now,
                        now,
                    ),
                )
                workspace_id = int(cur.lastrowid)
            self._conn.execute(
                """INSERT INTO discovery_progress
                   (workspace_id, phase, status, metadata_json, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(workspace_id) DO UPDATE SET
                     phase = excluded.phase, status = excluded.status,
                     metadata_json = excluded.metadata_json, updated_at = excluded.updated_at""",
                (workspace_id, actual_phase, actual_status, _json_text(metadata or {}, {}), now),
            )
        return workspace_id

    create_discovery_workspace = create_workspace
    create_scan_workspace = create_workspace

    def get_workspace(self, workspace_id: int | str) -> sqlite3.Row | None:
        if isinstance(workspace_id, int) and not isinstance(workspace_id, bool):
            return self._conn.execute(
                "SELECT * FROM discovery_workspaces WHERE id = ?", (workspace_id,)
            ).fetchone()
        return self._conn.execute(
            "SELECT * FROM discovery_workspaces WHERE workspace_key = ? OR id = ?",
            (str(workspace_id), int(str(workspace_id)) if str(workspace_id).isdigit() else -1),
        ).fetchone()

    def adopt_workspace_key(
        self,
        source_key: str,
        target_key: str,
        *,
        name: str = "",
    ) -> int | None:
        """Attach an anonymous workspace to a newly authenticated user.

        Anonymous source imports are created before Spotify login. Re-keying the
        existing parent row preserves every cascaded library/provenance row
        without copying chart candidates or tokens. If the user already has a
        workspace, it is left untouched and the existing user workspace ID is
        returned; callers can safely let the user import again in that rare
        collision case.
        """
        source_key = str(source_key or "")
        target_key = str(target_key or "")
        if not source_key or not target_key or source_key == target_key:
            row = self.get_workspace(target_key or source_key)
            return int(row["id"]) if row is not None else None
        with self._conn:
            source = self._conn.execute(
                "SELECT id, metadata_json FROM discovery_workspaces WHERE workspace_key = ?",
                (source_key,),
            ).fetchone()
            if source is None:
                target = self._conn.execute(
                    "SELECT id FROM discovery_workspaces WHERE workspace_key = ?",
                    (target_key,),
                ).fetchone()
                return int(target["id"]) if target is not None else None
            target = self._conn.execute(
                "SELECT id FROM discovery_workspaces WHERE workspace_key = ?",
                (target_key,),
            ).fetchone()
            if target is not None:
                return int(target["id"])
            metadata = _load_json(source["metadata_json"], {})
            if not isinstance(metadata, dict):
                metadata = {}
            metadata.pop("anonymous", None)
            metadata["adopted_from"] = source_key
            assignments = ["workspace_key = ?", "metadata_json = ?", "updated_at = ?"]
            values: list[Any] = [target_key, _json_text(metadata, {}), time.time()]
            if name:
                assignments.insert(1, "name = ?")
                values.insert(1, str(name))
            values.append(int(source["id"]))
            self._conn.execute(
                "UPDATE discovery_workspaces SET "
                + ", ".join(assignments)
                + " WHERE id = ?",
                values,
            )
            return int(source["id"])

    get_discovery_workspace = get_workspace
    get_workspace_by_key = get_workspace

    def list_workspaces(self, status: str | None = None) -> list[sqlite3.Row]:
        if status:
            return list(
                self._conn.execute(
                    "SELECT * FROM discovery_workspaces WHERE status = ? ORDER BY updated_at DESC",
                    (status,),
                )
            )
        return list(self._conn.execute("SELECT * FROM discovery_workspaces ORDER BY updated_at DESC"))

    def delete_workspace_by_key(self, workspace_key: str) -> int:
        """Delete one workspace and its cascaded workspace-scoped state."""
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM discovery_workspaces WHERE workspace_key = ?",
                (str(workspace_key),),
            )
        return int(cur.rowcount)

    def reset_workspace_if_idle(self, workspace_key: str, user_id: int) -> bool:
        """Atomically clear one workspace unless its owner has an active job."""
        key = str(workspace_key)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            if self.get_active_job(int(user_id), workspace_key=key) is not None:
                self._conn.rollback()
                return False
            row = self._conn.execute(
                "SELECT id FROM discovery_workspaces WHERE workspace_key = ?", (key,)
            ).fetchone()
            if row is None:
                self._conn.commit()
                return True
            workspace_id = int(row["id"])
            for table in (
                "workspace_selections",
                "workspace_candidates",
                "workspace_library_tracks",
                "workspace_expansion_batches",
                "workspace_expansion_artists",
                "spotify_item_statuses",
                "discovery_tasks",
                "discovery_scans",
            ):
                self._conn.execute(f"DELETE FROM {table} WHERE workspace_id = ?", (workspace_id,))
            now = time.time()
            self._conn.execute(
                """UPDATE discovery_workspaces SET status = 'ready_for_scope',
                   phase = 'ready_for_scope', total_tasks = 0, completed_tasks = 0,
                   failed_tasks = 0, total_tracks = 0, completed_tracks = 0,
                   source_count = 0, error_count = 0, progress_json = '{}',
                   completed_at = NULL, updated_at = ? WHERE id = ?""",
                (now, workspace_id),
            )
            self._conn.execute(
                """UPDATE discovery_progress SET phase = 'ready_for_scope',
                   status = 'ready_for_scope', total_tasks = 0, completed_tasks = 0,
                   failed_tasks = 0, total_tracks = 0, completed_tracks = 0,
                   message = '', error = NULL, metadata_json = '{}', updated_at = ?
                   WHERE workspace_id = ?""",
                (now, workspace_id),
            )
            self._conn.commit()
            return True
        except Exception:
            self._conn.rollback()
            raise

    def delete_workspace_if_idle(self, workspace_key: str, user_id: int) -> tuple[bool, bool]:
        """Atomically delete one idle workspace without touching global cache rows."""
        key = str(workspace_key)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            if self.get_active_job(int(user_id), workspace_key=key) is not None:
                self._conn.rollback()
                return False, False
            cursor = self._conn.execute(
                "DELETE FROM discovery_workspaces WHERE workspace_key = ?", (key,)
            )
            self._conn.execute(
                """UPDATE background_jobs SET workspace_key = '', updated_at = ?
                   WHERE user_id = ? AND workspace_key = ?
                     AND status NOT IN ('queued', 'running')""",
                (time.time(), int(user_id), key),
            )
            self._conn.commit()
            return True, bool(cursor.rowcount)
        except Exception:
            self._conn.rollback()
            raise

    def update_workspace(
        self,
        workspace_id: int | str,
        updates: Mapping[str, Any] | None = None,
        **values: Any,
    ) -> sqlite3.Row:
        db_id = self._workspace_db_id(workspace_id)
        data = dict(updates or {})
        data.update(values)
        progress_value = data.pop("progress", None)
        if "metadata" in data and "metadata_json" not in data:
            data["metadata_json"] = data.pop("metadata")
        allowed = {
            "name", "kind", "status", "phase", "spotify_account_id",
            "total_tasks", "completed_tasks", "failed_tasks", "total_tracks",
            "completed_tracks", "source_count", "error_count", "progress_json",
            "metadata_json", "completed_at",
        }
        assignments: list[str] = []
        params: list[Any] = []
        for key, value in data.items():
            if key not in allowed:
                continue
            if key in {"progress_json", "metadata_json"} and not isinstance(value, str):
                value = _json_text(value, {})
            assignments.append(f"{key} = ?")
            params.append(value)
        if data.get("status") in {"complete", "completed", "success", "failed", "error", "cancelled", "interrupted", "partial"} and "completed_at" not in data:
            assignments.append("completed_at = ?")
            params.append(time.time())
        if assignments:
            assignments.append("updated_at = ?")
            params.extend([time.time(), db_id])
            with self._conn:
                self._conn.execute(
                    "UPDATE discovery_workspaces SET " + ", ".join(assignments) + " WHERE id = ?",
                    params,
                )
        if progress_value is not None:
            self.update_workspace_progress(db_id, progress_value)
        row = self.get_workspace(db_id)
        assert row is not None
        return row

    update_discovery_workspace = update_workspace

    def update_workspace_progress(
        self,
        workspace_id: int | str,
        progress: Mapping[str, Any] | None = None,
        **values: Any,
    ) -> sqlite3.Row:
        db_id = self._workspace_db_id(workspace_id)
        data = dict(progress or {})
        data.update(values)
        aliases = {
            "total": "total_tasks",
            "completed": "completed_tasks",
            "tasks_done": "completed_tasks",
            "tasks_total": "total_tasks",
            "tasks_completed": "completed_tasks",
            "tasks_failed": "failed_tasks",
            "tracks_total": "total_tracks",
            "tracks_completed": "completed_tracks",
            "current_phase": "phase",
            "message": "message",
        }
        for old, new in aliases.items():
            if new not in data and old in data:
                data[new] = data[old]
        now = time.time()
        progress_columns = {
            "phase", "status", "total_tasks", "completed_tasks", "failed_tasks",
            "total_tracks", "completed_tracks", "message", "error",
        }
        workspace_columns = progress_columns - {"message", "error"}
        progress_assignments: list[str] = []
        progress_params: list[Any] = []
        for key in progress_columns:
            if key in data:
                progress_assignments.append(f"{key} = ?")
                progress_params.append(data[key])
        metadata_value = data.get("metadata", data.get("metadata_json"))
        if metadata_value is not None:
            progress_assignments.append("metadata_json = ?")
            progress_params.append(
                metadata_value if isinstance(metadata_value, str) else _json_text(metadata_value, {})
            )
        progress_assignments.append("updated_at = ?")
        progress_params.append(now)
        progress_params.append(db_id)
        with self._conn:
            self._conn.execute(
                """INSERT INTO discovery_progress (workspace_id, updated_at)
                   VALUES (?, ?)
                   ON CONFLICT(workspace_id) DO UPDATE SET updated_at = excluded.updated_at""",
                (db_id, now),
            )
            self._conn.execute(
                "UPDATE discovery_progress SET " + ", ".join(progress_assignments)
                + " WHERE workspace_id = ?",
                progress_params,
            )
            workspace_updates = {
                key: data[key] for key in workspace_columns if key in data
            }
            if "status" in data:
                workspace_updates["status"] = data["status"]
            if "phase" in data:
                workspace_updates["phase"] = data["phase"]
            if workspace_updates:
                self.update_workspace(db_id, workspace_updates)
        row = self._conn.execute(
            "SELECT * FROM discovery_progress WHERE workspace_id = ?", (db_id,)
        ).fetchone()
        assert row is not None
        return row

    update_scan_progress = update_workspace_progress
    set_workspace_progress = update_workspace_progress

    def get_workspace_progress(self, workspace_id: int | str) -> sqlite3.Row | None:
        db_id = self._workspace_db_id(workspace_id)
        return self._conn.execute(
            "SELECT * FROM discovery_progress WHERE workspace_id = ?", (db_id,)
        ).fetchone()

    get_scan_progress = get_workspace_progress

    def create_discovery_scan(
        self,
        workspace_id: int | str,
        scan: Mapping[str, Any] | str | None = None,
        *args: Any,
        **values: Any,
    ) -> int:
        db_id = self._workspace_db_id(workspace_id)
        data = _as_mapping(scan)
        if isinstance(scan, str):
            data = {"scan_key": scan}
            if args:
                data["kind"] = args[0]
            if len(args) > 1:
                data["status"] = args[1]
        data.update(values)
        scan_key = str(_first_value(data, "scan_key", "key", default=uuid.uuid4().hex) or "")
        kind = str(_first_value(data, "kind", default="exact") or "exact")
        status = str(_first_value(data, "status", default="created") or "created")
        now = time.time()
        finished_at = now if status in {"complete", "completed", "partial", "failed", "error", "cancelled", "interrupted"} else None
        with self._conn:
            self._conn.execute(
                """INSERT INTO discovery_scans
                   (workspace_id, scan_key, kind, status, started_at, finished_at, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(workspace_id, scan_key) DO UPDATE SET
                      kind = excluded.kind, status = excluded.status,
                      started_at = COALESCE(discovery_scans.started_at, excluded.started_at),
                      finished_at = excluded.finished_at,
                      metadata_json = excluded.metadata_json""",
                 (db_id, scan_key, kind, status, now, finished_at, _json_text(data.get("metadata", {}), {})),
            )
            row = self._conn.execute(
                "SELECT id FROM discovery_scans WHERE workspace_id = ? AND scan_key = ?",
                (db_id, scan_key),
            ).fetchone()
        return int(row["id"])

    def get_discovery_scans(self, workspace_id: int | str) -> list[sqlite3.Row]:
        db_id = self._workspace_db_id(workspace_id)
        return list(
            self._conn.execute(
                "SELECT * FROM discovery_scans WHERE workspace_id = ? ORDER BY id", (db_id,)
            )
        )

    get_scans = get_discovery_scans
    create_scan = create_discovery_scan

    def get_latest_combined_generation(self, workspace_id: int | str) -> sqlite3.Row | None:
        """Return the newest local combined generation, regardless of status."""
        db_id = self._workspace_db_id(workspace_id)
        return self._conn.execute(
            """SELECT * FROM discovery_scans
               WHERE workspace_id = ? AND kind = 'exact'
                 AND json_extract(metadata_json, '$.workflow') = 'combined_catalog'
               ORDER BY id DESC LIMIT 1""",
            (db_id,),
        ).fetchone()

    def get_current_combined_generation(self, workspace_id: int | str) -> sqlite3.Row | None:
        """Return the current generation only when the newest one completed."""
        row = self.get_latest_combined_generation(workspace_id)
        if row is None or str(row["status"] or "").lower() not in {"complete", "completed"}:
            return None
        return row

    def get_latest_exact_generation(self, workspace_id: int | str) -> sqlite3.Row | None:
        """Return the newest exact scan regardless of terminal state."""
        db_id = self._workspace_db_id(workspace_id)
        return self._conn.execute(
            """SELECT * FROM discovery_scans
               WHERE workspace_id = ? AND kind = 'exact'
               ORDER BY id DESC LIMIT 1""",
            (db_id,),
        ).fetchone()

    def get_current_exact_generation(self, workspace_id: int | str) -> sqlite3.Row | None:
        """Return the newest exact scan only when that scan completed."""
        row = self.get_latest_exact_generation(workspace_id)
        if row is None or str(row["status"] or "").lower() not in {"complete", "completed"}:
            return None
        return row

    def upsert_discovery_task(
        self,
        workspace_id: int | str,
        task: Mapping[str, Any] | Any | None = None,
        *args: Any,
        **values: Any,
    ) -> int:
        db_id = self._workspace_db_id(workspace_id)
        data = _as_mapping(task)
        if args:
            # Convenient positional form: (source, kind, query_key, query_text).
            positional = [task, *args]
            data = {
                "source": positional[0] if len(positional) > 0 else "",
                "kind": positional[1] if len(positional) > 1 else "song",
                "query_key": positional[2] if len(positional) > 2 else "",
                "query_text": positional[3] if len(positional) > 3 else "",
            }
        data.update(values)
        scan_id = data.get("scan_id")
        source = str(_first_value(data, "source", "source_name", default="") or "")
        kind = str(_first_value(data, "kind", default="song") or "song")
        query_key = str(_first_value(data, "query_key", "key", default="") or "")
        query_text = str(_first_value(data, "query_text", "query", default="") or "")
        status = str(_first_value(data, "status", default="pending") or "pending")
        now = time.time()
        fields_data = {
            "total": _first_value(data, "total", default=0) or 0,
            "completed": _first_value(data, "completed", default=0) or 0,
            "result_cap": _first_value(data, "result_cap", "cap", default=None),
            "source_total": _first_value(data, "source_total", default=None),
            "returned_count": _first_value(data, "returned_count", default=0) or 0,
            "cap_reached": int(bool(_first_value(data, "cap_reached", default=False))),
            "error": _first_value(data, "error", default=None),
            "metadata_json": _json_text(_first_value(data, "metadata", default={}), {}),
        }
        with self._conn:
            self._conn.execute(
                """INSERT INTO discovery_tasks
                   (workspace_id, scan_id, source, kind, query_key, query_text,
                    status, total, completed, result_cap, source_total,
                    returned_count, cap_reached, error, metadata_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(workspace_id, source, kind, query_key) DO UPDATE SET
                     scan_id = excluded.scan_id, query_text = excluded.query_text,
                     status = excluded.status, total = excluded.total,
                     completed = excluded.completed, result_cap = excluded.result_cap,
                     source_total = excluded.source_total,
                     returned_count = excluded.returned_count,
                     cap_reached = excluded.cap_reached, error = excluded.error,
                     metadata_json = excluded.metadata_json""",
                (
                    db_id,
                    scan_id,
                    source,
                    kind,
                    query_key,
                    query_text,
                    status,
                    fields_data["total"],
                    fields_data["completed"],
                    fields_data["result_cap"],
                    fields_data["source_total"],
                    fields_data["returned_count"],
                    fields_data["cap_reached"],
                    fields_data["error"],
                    fields_data["metadata_json"],
                    now,
                ),
            )
            row = self._conn.execute(
                """SELECT id FROM discovery_tasks
                   WHERE workspace_id = ? AND source = ? AND kind = ? AND query_key = ?""",
                (db_id, source, kind, query_key),
            ).fetchone()
        return int(row["id"])

    create_discovery_task = upsert_discovery_task

    def update_discovery_task(
        self,
        task_id: int,
        updates: Mapping[str, Any] | None = None,
        **values: Any,
    ) -> sqlite3.Row:
        data = dict(updates or {})
        data.update(values)
        allowed = {
            "scan_id", "status", "total", "completed", "result_cap", "source_total",
            "returned_count", "cap_reached", "error", "metadata_json", "started_at",
            "finished_at",
        }
        assignments: list[str] = []
        params: list[Any] = []
        for key, value in data.items():
            if key not in allowed:
                continue
            if key == "metadata_json" and not isinstance(value, str):
                value = _json_text(value, {})
            assignments.append(f"{key} = ?")
            params.append(value)
        if "status" in data and data["status"] in {"running", "started"}:
            assignments.append("started_at = COALESCE(started_at, ?)")
            params.append(time.time())
        if "status" in data and data["status"] in {"complete", "completed", "error", "failed", "cancelled", "interrupted", "partial"}:
            assignments.append("finished_at = ?")
            params.append(time.time())
        if assignments:
            params.append(task_id)
            with self._conn:
                self._conn.execute(
                    "UPDATE discovery_tasks SET " + ", ".join(assignments) + " WHERE id = ?",
                    params,
                )
        row = self._conn.execute("SELECT * FROM discovery_tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown discovery task: {task_id}")
        return row

    def get_discovery_tasks(
        self,
        workspace_id: int | str,
        *,
        status: str | None = None,
    ) -> list[sqlite3.Row]:
        db_id = self._workspace_db_id(workspace_id)
        if status:
            return list(
                self._conn.execute(
                    "SELECT * FROM discovery_tasks WHERE workspace_id = ? AND status = ? ORDER BY id",
                    (db_id, status),
                )
            )
        return list(
            self._conn.execute(
                "SELECT * FROM discovery_tasks WHERE workspace_id = ? ORDER BY id", (db_id,)
            )
        )

    list_discovery_tasks = get_discovery_tasks

    # --------------------------------------------------- workspace candidates
    def _candidate_db_id(self, value: Any) -> int:
        if isinstance(value, (tuple, list)) and len(value) == 2:
            row = self._conn.execute(
                "SELECT id FROM candidate_charts WHERE source = ? AND chart_id = ?",
                (str(value[0]), value[1]),
            ).fetchone()
            if row:
                return int(row["id"])
        if isinstance(value, int) and not isinstance(value, bool):
            row = self._conn.execute(
                "SELECT id FROM candidate_charts WHERE id = ?", (value,)
            ).fetchone()
            if row:
                return int(row["id"])
        if isinstance(value, str) and value.strip().isdigit():
            row = self._conn.execute(
                "SELECT id FROM candidate_charts WHERE id = ?", (int(value.strip()),)
            ).fetchone()
            if row:
                return int(row["id"])
        value_data = _as_mapping(value)
        source = value_data.get("source")
        chart_id = value_data.get("chart_id", value_data.get("chartId"))
        if source is not None and chart_id is not None:
            row = self._conn.execute(
                "SELECT id FROM candidate_charts WHERE source = ? AND chart_id = ?",
                (str(source), chart_id),
            ).fetchone()
            if row:
                return int(row["id"])
        candidate = _coerce_candidate(value)
        if candidate.db_id is None:
            candidate = self.upsert_candidate(candidate)
        return int(candidate.db_id)

    def _resolve_track_value(self, value: Any) -> int | None:
        track_id = self._track_db_id(value)
        if track_id is not None:
            return track_id
        data = _as_mapping(value)
        nested = data.get("track")
        if nested is not None:
            track_id = self._track_db_id(nested)
        if track_id is not None:
            return track_id
        if data and (data.get("spotify_id") or data.get("track_id") or data.get("title")):
            _, _, _, track_id = self._upsert_spotify_track(value)
            return track_id
        return None

    @staticmethod
    def _candidate_quality(value: Any, breakdown: Any = None) -> float:
        data = _as_mapping(value)
        raw = _first_value(data, "quality", "quality_score", default=None)
        if raw is None and isinstance(breakdown, Mapping):
            parsed = breakdown
            raw = parsed.get("quality")
        elif raw is None and isinstance(breakdown, str):
            parsed = _load_json(breakdown, {})
            if isinstance(parsed, Mapping):
                raw = parsed.get("quality")
        try:
            return float(raw or 0)
        except (TypeError, ValueError):
            return 0.0

    def upsert_workspace_candidate(
        self,
        workspace_id: int | str,
        track_id: Any = None,
        candidate: Any = None,
        score: float | None = None,
        decision: str = "",
        breakdown_json: str | Mapping[str, Any] = "",
        quality: float | None = None,
        eligibility: str = "",
        kind: str = "exact",
        artist_norm: str = "",
        task_id: int | None = None,
        metadata: Mapping[str, Any] | None = None,
        candidate_id: Any = None,
        breakdown: Any = None,
    ) -> int:
        db_id = self._workspace_db_id(workspace_id)
        if candidate is None and isinstance(track_id, Mapping):
            row_data = dict(track_id)
            candidate = row_data.get("candidate") or row_data.get("chart") or row_data.get("candidate_json")
            track_id = row_data.get("track_id") or row_data.get("spotify_id") or row_data.get("track")
            score = row_data.get("score", score)
            quality = row_data.get("quality", quality)
            decision = row_data.get("decision", decision)
            eligibility = row_data.get("eligibility", row_data.get("state", eligibility))
            breakdown_json = row_data.get("breakdown_json", row_data.get("breakdown", breakdown_json))
            kind = row_data.get("kind", kind)
            artist_norm = row_data.get("artist_norm", artist_norm)
            task_id = row_data.get("task_id", task_id)
        if candidate is None:
            candidate = candidate_id
        if candidate is None:
            raise ValueError("workspace candidate is required")
        if not breakdown_json and breakdown is not None:
            breakdown_json = breakdown
        if isinstance(candidate, str):
            candidate = _load_json(candidate, {})
        candidate_id = self._candidate_db_id(candidate)
        resolved_track_id = self._resolve_track_value(track_id)
        candidate_data = _as_mapping(candidate)
        if score is None:
            raw_score = _first_value(candidate_data, "score", "final_score", default=0)
            try:
                score = float(raw_score or 0)
            except (TypeError, ValueError):
                score = 0.0
        if quality is None:
            quality = self._candidate_quality(candidate, breakdown_json)
        if isinstance(breakdown_json, Mapping):
            breakdown_text = _json_text(breakdown_json, {})
        else:
            breakdown_text = str(breakdown_json or "")
        if not breakdown_text:
            breakdown_text = "{}"
        if not eligibility:
            eligibility = str(
                _first_value(candidate_data, "eligibility", "export_state", default="") or ""
            )
        if not decision:
            decision = str(_first_value(candidate_data, "decision", default="") or "")
        artist_norm = artist_norm or str(
            _first_value(candidate_data, "artist_norm", default="") or ""
        )
        artist_norm = artist_norm if kind != "exact" else ""
        with self._conn:
            self._conn.execute(
                """INSERT INTO workspace_candidates
                   (workspace_id, track_id, candidate_id, kind, artist_norm, task_id,
                    score, quality, decision, eligibility, breakdown_json, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(workspace_id, track_id, candidate_id, kind, artist_norm)
                   DO UPDATE SET task_id = excluded.task_id,
                     score = excluded.score, quality = excluded.quality,
                     decision = excluded.decision, eligibility = excluded.eligibility,
                     breakdown_json = excluded.breakdown_json,
                     metadata_json = excluded.metadata_json""",
                (
                    db_id,
                    resolved_track_id,
                    candidate_id,
                    kind,
                    artist_norm,
                    task_id,
                    score,
                    quality,
                    decision,
                    eligibility,
                    breakdown_text,
                    _json_text(metadata or {}, {}),
                ),
            )
            row = self._conn.execute(
                """SELECT id FROM workspace_candidates
                   WHERE workspace_id = ? AND candidate_id = ? AND kind = ?
                     AND COALESCE(track_id, 0) = COALESCE(?, 0)
                     AND artist_norm = ?""",
                (db_id, candidate_id, kind, resolved_track_id, artist_norm),
            ).fetchone()
            if task_id is not None:
                self._conn.execute(
                    "INSERT OR IGNORE INTO workspace_task_candidates (task_id, candidate_id) VALUES (?, ?)",
                    (task_id, candidate_id),
                )
            self._conn.execute("UPDATE discovery_workspaces SET candidate_revision=candidate_revision+1, updated_at=? WHERE id=?", (time.time(), db_id))
        return int(row["id"])

    record_workspace_candidate = upsert_workspace_candidate
    record_workspace_match = upsert_workspace_candidate
    persist_workspace_candidate = upsert_workspace_candidate

    def assign_workspace_track_candidates_to_task(
        self, workspace_id: int | str, track_id: Any, task_id: int
    ) -> int:
        """Attach every persisted relationship for a track to one run task."""
        db_id = self._workspace_db_id(workspace_id)
        resolved = self._resolve_track_value(track_id)
        with self._conn:
            cur = self._conn.execute(
                "UPDATE workspace_candidates SET task_id = ? "
                "WHERE workspace_id = ? AND track_id = ? AND kind = 'exact'",
                (int(task_id), db_id, resolved),
            )
        return int(cur.rowcount)

    def clear_workspace_candidates(
        self,
        workspace_id: int | str,
        kind: str | None = None,
    ) -> int:
        """Discard workspace candidate results and their manual selections.

        If kind is specified (e.g. 'expansion'), only removes candidates/selections of that kind.
        Otherwise removes all candidates and selections for the workspace.
        """
        db_id = self._workspace_db_id(workspace_id)
        with self._conn:
            if kind:
                self._conn.execute(
                    "DELETE FROM workspace_selections WHERE workspace_id = ? AND kind = ?",
                    (db_id, kind),
                )
                cur = self._conn.execute(
                    "DELETE FROM workspace_candidates WHERE workspace_id = ? AND kind = ?",
                    (db_id, kind),
                )
            else:
                self._conn.execute(
                    "DELETE FROM workspace_selections WHERE workspace_id = ?",
                    (db_id,),
                )
                cur = self._conn.execute(
                    "DELETE FROM workspace_candidates WHERE workspace_id = ?",
                    (db_id,),
                )
            if cur.rowcount:
                self._conn.execute("UPDATE discovery_workspaces SET candidate_revision=candidate_revision+1, updated_at=? WHERE id=?", (time.time(), db_id))
        return int(cur.rowcount)

    def clear_workspace_expansion_candidates(self, workspace_id: int | str) -> int:
        """Discard prior expansion results and their manual selections.

        Expansion is an explicitly confirmed, derived Spotify queue.  A fresh
        queue must not leave stale artist-wide results selectable alongside
        the new per-song results.
        """
        return self.clear_workspace_candidates(workspace_id, kind="expansion")

    def upsert_workspace_candidates(
        self,
        workspace_id: int | str,
        candidates: Iterable[Any],
        **defaults: Any,
    ) -> list[int]:
        ids: list[int] = []
        with self._conn:
            for value in candidates:
                data = dict(defaults)
                row_data = _as_mapping(value)
                if row_data:
                    data.update(row_data)
                candidate = (
                    data.pop("candidate", None)
                    or data.pop("chart", None)
                    or data.pop("candidate_id", None)
                    or value
                )
                track_id = data.pop("track_id", data.pop("track", None))
                ids.append(
                    self.upsert_workspace_candidate(
                        workspace_id,
                        track_id,
                        candidate,
                        **{key: value for key, value in data.items() if key in {
                            "score", "quality", "decision", "eligibility", "breakdown_json",
                            "kind", "artist_norm", "task_id", "metadata",
                        }},
                    )
                )
        return ids

    def get_workspace_candidates(
        self,
        workspace_id: int | str,
        track_id: Any | None = None,
        *,
        kind: str | None = None,
        include_rejected: bool = True,
        include_candidate_data: bool = True,
        current_only: bool = False,
        generation_id: int | None = None,
    ) -> list[sqlite3.Row]:
        db_id = self._workspace_db_id(workspace_id)
        clauses = ["wc.workspace_id = ?"]
        params: list[Any] = [db_id]
        resolved = self._resolve_track_value(track_id) if track_id is not None else None
        if track_id is not None:
            clauses.append("wc.track_id = ?")
            params.append(resolved)
        if kind:
            clauses.append("wc.kind = ?")
            params.append(kind)
        if current_only:
            generation = self.get_current_combined_generation(db_id)
            clauses.append(
                "EXISTS (SELECT 1 FROM discovery_tasks dt "
                "JOIN discovery_scans ds ON ds.id = dt.scan_id "
                "WHERE dt.id = wc.task_id AND ds.workspace_id = wc.workspace_id "
                "AND ds.kind = 'exact' AND ds.id = ?)"
            )
            params.append(int(generation["id"]) if generation is not None else -1)
        elif generation_id is not None:
            clauses.append(
                "EXISTS (SELECT 1 FROM discovery_tasks dt "
                "JOIN discovery_scans ds ON ds.id = dt.scan_id "
                "WHERE dt.id = wc.task_id AND ds.workspace_id = wc.workspace_id "
                "AND ds.kind = 'exact' AND ds.id = ?)"
            )
            params.append(int(generation_id))
        if not include_rejected:
            clauses.append("COALESCE(LOWER(wc.decision), '') NOT IN ('reject', 'rejected', 'incompatible')")
        candidate_json = "c.data_json AS candidate_json" if include_candidate_data else "'' AS candidate_json"
        return list(
            self._conn.execute(
                f"""SELECT wc.*, c.source, c.chart_id, {candidate_json},
                          t.track_key, t.spotify_id, t.title AS track_title,
                          t.artist_full AS track_artist,
                          EXISTS (SELECT 1 FROM workspace_selections ws
                                  WHERE ws.workspace_id = wc.workspace_id
                                    AND (ws.track_id = wc.track_id
                                         OR (ws.track_id IS NULL AND wc.track_id IS NULL))
                                    AND ws.candidate_id = wc.candidate_id
                                    AND ws.kind = wc.kind
                                    AND COALESCE(ws.artist_norm, '') = COALESCE(wc.artist_norm, '')
                                    AND ws.status = 'selected') AS selected
                   FROM workspace_candidates wc
                   JOIN candidate_charts c ON c.id = wc.candidate_id
                   LEFT JOIN spotify_tracks t ON t.id = wc.track_id
                   WHERE """ + " AND ".join(clauses) +
                " ORDER BY COALESCE(wc.track_id, 0), wc.score DESC, wc.quality DESC, c.source, c.chart_id",
                params,
            )
        )

    def get_workspace_candidate_groups(
        self,
        workspace_id: int | str,
        *,
        kind: str = "exact",
        include_rejected: bool = True,
        current_only: bool = False,
    ) -> dict[str, list[sqlite3.Row]]:
        groups: dict[str, list[sqlite3.Row]] = {}
        for row in self.get_workspace_candidates(
            workspace_id, kind=kind, include_rejected=include_rejected,
            current_only=current_only,
        ):
            key = row["spotify_id"] or row["track_key"] or str(row["track_id"])
            groups.setdefault(str(key), []).append(row)
        return groups

    def get_workspace_candidates_for_tracks(
        self,
        workspace_id: int | str,
        track_ids: Iterable[int],
        *,
        kind: str = "exact",
        include_rejected: bool = False,
        current_only: bool = False,
    ) -> dict[int, list[sqlite3.Row]]:
        """Load candidates for one rendered track page in a single query."""
        ids = sorted({int(value) for value in track_ids})
        if not ids:
            return {}
        db_id = self._workspace_db_id(workspace_id)
        placeholders = ",".join("?" for _ in ids)
        clauses = ["wc.workspace_id = ?", f"wc.track_id IN ({placeholders})", "wc.kind = ?"]
        params: list[Any] = [db_id, *ids, kind]
        if not include_rejected:
            clauses.append("COALESCE(LOWER(wc.decision), '') NOT IN ('reject', 'rejected', 'incompatible')")
        if current_only:
            generation = self.get_current_combined_generation(db_id)
            clauses.append(
                "EXISTS (SELECT 1 FROM discovery_tasks dt JOIN discovery_scans ds ON ds.id = dt.scan_id "
                "WHERE dt.id = wc.task_id AND ds.workspace_id = wc.workspace_id AND ds.kind = 'exact' "
                "AND ds.id = ?)"
            )
            params.append(int(generation["id"]) if generation is not None else -1)
        rows = self._conn.execute(
            """SELECT wc.*, c.source, c.chart_id, c.data_json AS candidate_json,
                      t.track_key, t.spotify_id, t.title AS track_title,
                      t.artist_full AS track_artist,
                      EXISTS (SELECT 1 FROM workspace_selections ws
                              WHERE ws.workspace_id = wc.workspace_id
                                AND ws.track_id = wc.track_id
                                AND ws.candidate_id = wc.candidate_id
                                AND ws.kind = wc.kind
                                AND COALESCE(ws.artist_norm, '') = COALESCE(wc.artist_norm, '')
                                AND ws.status = 'selected') AS selected
               FROM workspace_candidates wc
               JOIN candidate_charts c ON c.id = wc.candidate_id
               LEFT JOIN spotify_tracks t ON t.id = wc.track_id
               WHERE """ + " AND ".join(clauses) +
            " ORDER BY wc.track_id, wc.score DESC, wc.quality DESC, c.source, c.chart_id",
            params,
        )
        grouped: dict[int, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(int(row["track_id"]), []).append(row)
        return grouped

    group_workspace_candidates = get_workspace_candidate_groups
    get_candidate_groups = get_workspace_candidate_groups
    get_candidates_for_track = get_workspace_candidates

    def add_workspace_candidates(
        self,
        workspace_id: int | str,
        track_id: Any,
        candidates: Iterable[Any],
        **defaults: Any,
    ) -> list[int]:
        ids: list[int] = []
        for value in candidates:
            data = _as_mapping(value)
            candidate = data.get("candidate") or data.get("chart") or value
            options = dict(defaults)
            options.update({key: data[key] for key in (
                "score", "quality", "decision", "eligibility", "breakdown_json",
                "kind", "artist_norm", "task_id", "metadata",
            ) if key in data})
            ids.append(self.upsert_workspace_candidate(workspace_id, track_id, candidate, **options))
        return ids

    # ---------------------------------------------------------- selections
    def select_workspace_candidate(
        self,
        workspace_id: int | str,
        track_id: Any,
        candidate: Any = None,
        kind: str = "exact",
        artist_norm: str = "",
        score: float | None = None,
        explanation: str = "",
        eligibility: str = "",
        status: str = "selected",
        planned_bytes: int = 0,
        target_relpath: str = "",
        metadata: Mapping[str, Any] | None = None,
        candidate_id: Any = None,
        force: bool = False,
        require_workspace_candidate: bool = False,
    ) -> int:
        db_id = self._workspace_db_id(workspace_id)
        if candidate is None:
            candidate = candidate_id
        if candidate is None:
            raise ValueError("candidate is required")
        resolved_track = self._resolve_track_value(track_id)
        candidate_id = self._candidate_db_id(candidate)
        candidate_row = self.get_candidate(candidate_id)
        if candidate_row is not None and not self.is_chart_artifact_eligible(
            candidate_row.source, candidate_row.chart_id
        ):
            raise ValueError("candidate artifact is rejected or unavailable")
        if resolved_track is None and kind == "exact":
            raise ValueError("exact selection requires a Spotify track")
        if kind == "exact":
            check = self._conn.execute(
                """SELECT eligibility, decision, score, breakdown_json FROM workspace_candidates
                   WHERE workspace_id = ? AND track_id = ? AND candidate_id = ?
                   ORDER BY id DESC LIMIT 1""",
                (db_id, resolved_track, candidate_id),
            ).fetchone()
        else:
            check = self._conn.execute(
                """SELECT eligibility, decision, score, breakdown_json FROM workspace_candidates
                   WHERE workspace_id = ? AND candidate_id = ? AND kind = ?
                   ORDER BY id DESC LIMIT 1""",
                (db_id, candidate_id, kind),
            ).fetchone()
        if check is None and require_workspace_candidate:
            raise ValueError("candidate does not belong to this workspace track")
        if check:
            breakdown = _load_json(check["breakdown_json"], {})
            gate_factors = breakdown.get("gate_factors", {}) if isinstance(breakdown, Mapping) else {}
            factors = breakdown.get("factors", {}) if isinstance(breakdown, Mapping) else {}
            nested_gates = factors.get("gates", {}) if isinstance(factors, Mapping) else {}
            if any(
                isinstance(gates, Mapping) and gates.get("hard_gate")
                for gates in (gate_factors, nested_gates)
            ):
                raise ValueError("candidate is blocked by a hard compatibility gate")
            blocked = {"excluded", "incompatible", "reject", "rejected", "not_exportable"}
            if str(check["eligibility"] or "").lower() in blocked or str(check["decision"] or "").lower() in blocked:
                if not force or str(check["eligibility"] or "").lower() == "incompatible":
                    # A user may override a soft EXCLUDED (low-score) chart, but
                    # never a hard INCOMPATIBLE one (wrong artist / no guitar).
                    raise ValueError("candidate is not eligible for selection")
                eligibility = "ELIGIBLE_REVIEW"
            if not eligibility:
                eligibility = check["eligibility"] or ""
            if score is None:
                score = check["score"]
            if not explanation:
                explanation = check["breakdown_json"] or ""
        if score is None and candidate_row is not None:
            score = 0.0
        now = time.time()
        with self._conn:
            if kind == "exact":
                self._conn.execute(
                    """DELETE FROM workspace_selections
                       WHERE workspace_id = ? AND kind = 'exact' AND track_id = ?""",
                    (db_id, resolved_track),
                )
            else:
                self._conn.execute(
                    """DELETE FROM workspace_selections
                       WHERE workspace_id = ? AND kind = ? AND candidate_id = ?
                         AND COALESCE(artist_norm, '') = COALESCE(?, '')
                         AND COALESCE(track_id, 0) = COALESCE(?, 0)""",
                    (db_id, kind, candidate_id, artist_norm, resolved_track),
                )
            row_track = self._conn.execute(
                "SELECT spotify_id FROM spotify_tracks WHERE id = ?", (resolved_track,)
            ).fetchone() if resolved_track is not None else None
            cur = self._conn.execute(
                """INSERT INTO workspace_selections
                   (workspace_id, kind, track_id, spotify_track_id, artist_norm,
                    candidate_id, score, explanation, eligibility, status,
                    planned_bytes, target_relpath, selected_at, updated_at, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    db_id,
                    kind,
                    resolved_track,
                    row_track["spotify_id"] if row_track else None,
                    artist_norm,
                    candidate_id,
                    score,
                    explanation,
                    eligibility,
                    status,
                    planned_bytes,
                    target_relpath,
                    now,
                    now,
                    _json_text(metadata or {}, {}),
                ),
            )
        return int(cur.lastrowid)

    select_candidate = select_workspace_candidate
    select_chart = select_workspace_candidate

    def clear_workspace_selection(
        self,
        workspace_id: int | str,
        track_id: Any | None = None,
        *,
        candidate_id: int | None = None,
        selection_id: int | None = None,
        kind: str | None = None,
        artist_norm: str = "",
    ) -> int:
        db_id = self._workspace_db_id(workspace_id)
        clauses = ["workspace_id = ?"]
        params: list[Any] = [db_id]
        if track_id is not None:
            clauses.append("track_id = ?")
            params.append(self._resolve_track_value(track_id))
        if candidate_id is not None:
            clauses.append("candidate_id = ?")
            params.append(candidate_id)
        if selection_id is not None:
            clauses.append("id = ?")
            params.append(selection_id)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if artist_norm:
            clauses.append("artist_norm = ?")
            params.append(artist_norm)
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM workspace_selections WHERE " + " AND ".join(clauses), params
            )
        return int(cur.rowcount)

    def clear_selection(
        self,
        workspace_id: int | str,
        track_id: Any | None = None,
        *,
        candidate_id: int | None = None,
        selection_id: int | None = None,
        kind: str | None = None,
        artist_norm: str = "",
    ) -> int:
        # A few callers naturally pass a candidate id as the second positional
        # argument.  Prefer track identity when it resolves, otherwise treat a
        # known candidate id as the requested target.
        if candidate_id is None and track_id is not None:
            resolved_track = self._resolve_track_value(track_id)
            if resolved_track is None and isinstance(track_id, int):
                candidate_id = track_id
                track_id = None
        return self.clear_workspace_selection(
            workspace_id,
            track_id,
            candidate_id=candidate_id,
            selection_id=selection_id,
            kind=kind,
            artist_norm=artist_norm,
        )

    deselect_candidate = clear_selection

    def select_recommended(
        self,
        workspace_id: int | str,
        *,
        track_ids: Iterable[Any] | None = None,
        kind: str = "exact",
        minimum_score: float | None = None,
    ) -> list[int]:
        db_id = self._workspace_db_id(workspace_id)
        rows = self.get_workspace_candidates(db_id, kind=kind, include_rejected=False)
        requested: set[int] | None = None
        if track_ids is not None:
            requested = {
                resolved for item in track_ids
                if (resolved := self._resolve_track_value(item)) is not None
            }
        grouped: dict[Any, sqlite3.Row] = {}
        blocked = {"excluded", "incompatible", "reject", "rejected", "not_exportable"}
        for row in rows:
            if requested is not None and row["track_id"] not in requested:
                continue
            if str(row["eligibility"] or "").lower() in blocked:
                continue
            if str(row["decision"] or "").lower() in blocked:
                continue
            if minimum_score is not None and (row["score"] or 0) < minimum_score:
                continue
            key = row["track_id"] if kind == "exact" else (
                row["artist_norm"], row["candidate_id"]
            )
            previous = grouped.get(key)
            if previous is None or (
                row["score"] or 0,
                row["quality"] or 0,
                -row["candidate_id"],
            ) > (
                previous["score"] or 0,
                previous["quality"] or 0,
                -previous["candidate_id"],
            ):
                grouped[key] = row
        selected: list[int] = []
        for row in grouped.values():
            selected.append(
                self.select_workspace_candidate(
                    db_id,
                    row["track_id"] or row["spotify_id"] or row["track_key"],
                    row["candidate_id"],
                    kind=kind,
                    artist_norm=row["artist_norm"] or "",
                    score=row["score"],
                    explanation=row["breakdown_json"] or "",
                    eligibility=row["eligibility"] or "",
                )
            )
        return selected

    select_recommended_candidates = select_recommended
    select_recommended_for_workspace = select_recommended

    def list_workspace_selections(
        self,
        workspace_id: int | str,
        *,
        kind: str | None = None,
        status: str | None = "selected",
    ) -> list[sqlite3.Row]:
        db_id = self._workspace_db_id(workspace_id)
        clauses = ["s.workspace_id = ?"]
        params: list[Any] = [db_id]
        if kind:
            clauses.append("s.kind = ?")
            params.append(kind)
        if status:
            clauses.append("s.status = ?")
            params.append(status)
        return list(
            self._conn.execute(
                """SELECT s.*, c.source, c.chart_id, c.data_json AS candidate_json,
                          t.track_key, t.spotify_id, t.title AS track_title,
                          t.artist_full AS track_artist
                   FROM workspace_selections s
                   JOIN candidate_charts c ON c.id = s.candidate_id
                   LEFT JOIN spotify_tracks t ON t.id = s.track_id
                   WHERE """ + " AND ".join(clauses) + " ORDER BY s.selected_at, s.id",
                params,
            )
        )

    list_selected = list_workspace_selections
    get_workspace_selections = list_workspace_selections
    list_selected_items = list_workspace_selections

    def clear_workspace_folder_selection(
        self,
        workspace_id: int | str,
        folder_id: str | int | None = None,
        *,
        liked: bool = False,
    ) -> int:
        db_id = self._workspace_db_id(workspace_id)
        rows = self.get_library_track_rows(folder_id, liked=liked)
        ids = [row["id"] for row in rows]
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM workspace_selections WHERE workspace_id = ? AND track_id IN ("
                + placeholders
                + ")",
                [db_id, *ids],
            )
        return int(cur.rowcount)

    clear_folder_selection = clear_workspace_folder_selection

    def select_all_recommended(
        self,
        workspace_id: int | str,
        folder_id: str | int | None = None,
        *,
        liked: bool = False,
        minimum_score: float | None = None,
    ) -> list[int]:
        rows = self.get_library_track_rows(folder_id, liked=liked)
        return self.select_recommended(
            workspace_id,
            track_ids=[row["id"] for row in rows],
            minimum_score=minimum_score,
        )

    select_folder_recommended = select_all_recommended

    def replace_workspace_selections(
        self,
        workspace_id: int | str,
        selections: Iterable[Any],
    ) -> list[int]:
        db_id = self._workspace_db_id(workspace_id)
        with self._conn:
            self._conn.execute("DELETE FROM workspace_selections WHERE workspace_id = ?", (db_id,))
            ids: list[int] = []
            for value in selections:
                data = _as_mapping(value)
                candidate = data.get("candidate") or data.get("candidate_id")
                track = data.get("track_id") or data.get("spotify_track_id") or data.get("track")
                if candidate is None:
                    raise ValueError("selection requires candidate")
                ids.append(
                    self.select_workspace_candidate(
                        db_id,
                        track,
                        candidate,
                        kind=str(data.get("kind") or "exact"),
                        artist_norm=str(data.get("artist_norm") or ""),
                        score=data.get("score"),
                        explanation=str(data.get("explanation") or ""),
                        eligibility=str(data.get("eligibility") or ""),
                        status=str(data.get("status") or "selected"),
                        planned_bytes=int(data.get("planned_bytes") or 0),
                        target_relpath=str(data.get("target_relpath") or ""),
                        metadata=data.get("metadata") if isinstance(data.get("metadata"), Mapping) else {},
                    )
                )
        return ids

    # ----------------------------------------------------- expansion choices
    def replace_workspace_expansion_artists(
        self,
        workspace_id: int | str,
        artists: Iterable[Mapping[str, Any] | Any],
    ) -> list[int]:
        db_id = self._workspace_db_id(workspace_id)
        values = list(artists)
        norms = [
            str(_first_value(_as_mapping(value), "artist_norm", "normalized", default="") or "")
            for value in values
        ]
        ids: list[int] = []
        with self._conn:
            if norms:
                placeholders = ",".join("?" for _ in norms)
                self._conn.execute(
                    f"DELETE FROM workspace_expansion_artists WHERE workspace_id = ? "
                    f"AND artist_norm NOT IN ({placeholders})",
                    (db_id, *norms),
                )
            else:
                self._conn.execute(
                    "DELETE FROM workspace_expansion_artists WHERE workspace_id = ?",
                    (db_id,),
                )
            for value in values:
                data = _as_mapping(value)
                name = str(_first_value(data, "artist_name", "name", "artist", default="") or "")
                norm = str(_first_value(data, "artist_norm", "normalized", default=name) or name)
                cur = self._conn.execute(
                    """INSERT INTO workspace_expansion_artists
                       (workspace_id, artist_name, artist_norm, track_count, cap, selected, metadata_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(workspace_id, artist_norm) DO UPDATE SET
                         artist_name = excluded.artist_name,
                         track_count = excluded.track_count,
                         cap = excluded.cap,
                         metadata_json = excluded.metadata_json""",
                    (
                        db_id,
                        name,
                        norm,
                        int(_first_value(data, "track_count", "count", default=0) or 0),
                        int(_first_value(data, "cap", default=0) or 0),
                        int(bool(_first_value(data, "selected", "enabled", default=False))),
                        _json_text(_first_value(data, "metadata", default={}), {}),
                    ),
                )
                row = self._conn.execute(
                    """SELECT id FROM workspace_expansion_artists
                       WHERE workspace_id = ? AND artist_norm = ?""",
                    (db_id, norm),
                ).fetchone()
                ids.append(int(row["id"]) if row else int(cur.lastrowid))
        return ids

    def clear_workspace_expansion_artists(self, workspace_id: int | str) -> int:
        """Discard expansion artist choices and cached discography previews."""
        db_id = self._workspace_db_id(workspace_id)
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM workspace_expansion_artists WHERE workspace_id = ?",
                (db_id,),
            )
        return int(cur.rowcount)

    def clear_workspace_track_candidates(self, workspace_id: int | str, track_key: str) -> int:
        """Remove stale exact results for one changed track before recomputing it."""
        db_id = self._workspace_db_id(workspace_id)
        with self._conn:
            self._conn.execute(
                "DELETE FROM workspace_selections WHERE workspace_id=? AND kind='exact' AND track_id=(SELECT id FROM spotify_tracks WHERE track_key=?)",
                (db_id, str(track_key)),
            )
            cur = self._conn.execute(
                "DELETE FROM workspace_candidates WHERE workspace_id=? AND kind='exact' AND track_id=(SELECT id FROM spotify_tracks WHERE track_key=?)",
                (db_id, str(track_key)),
            )
            if cur.rowcount:
                self._conn.execute("UPDATE discovery_workspaces SET candidate_revision=candidate_revision+1, updated_at=? WHERE id=?", (time.time(), db_id))
        return int(cur.rowcount)

    def workspace_has_track_candidates(self, workspace_id: int | str, track_key: str) -> bool:
        db_id = self._workspace_db_id(workspace_id)
        return self._conn.execute(
            "SELECT 1 FROM workspace_candidates WHERE workspace_id=? AND kind='exact' AND track_id=(SELECT id FROM spotify_tracks WHERE track_key=?) LIMIT 1",
            (db_id, str(track_key)),
        ).fetchone() is not None

    def workspace_exact_candidate_count(self, workspace_id: int | str) -> int:
        db_id = self._workspace_db_id(workspace_id)
        row = self._conn.execute("SELECT COUNT(*) AS count FROM workspace_candidates WHERE workspace_id=? AND kind='exact'", (db_id,)).fetchone()
        return int(row["count"] or 0)

    def workspace_current_candidate_digest(self, workspace_id: int | str, track_key: str | None = None) -> tuple[int, str, set[tuple[str, int]]]:
        """Digest exact relationships limited to the current progress tracks."""
        db_id = self._workspace_db_id(workspace_id)
        params: list[Any] = [db_id]
        clause = ""
        if track_key is not None:
            clause = " AND t.track_key=?"
            params.append(str(track_key))
        rows = self._conn.execute(
            """SELECT t.track_key,wc.candidate_id,c.source,c.chart_id,wc.score,wc.quality,wc.decision,wc.eligibility,wc.breakdown_json,wc.metadata_json
               FROM workspace_candidates wc JOIN spotify_tracks t ON t.id=wc.track_id
               JOIN candidate_charts c ON c.id=wc.candidate_id
               JOIN discovery_song_progress dsp ON dsp.workspace_id=wc.workspace_id AND dsp.track_key=t.track_key
               WHERE wc.workspace_id=? AND wc.kind='exact'""" + clause + " ORDER BY t.track_key,wc.candidate_id,wc.score,wc.quality,wc.decision,wc.eligibility,wc.breakdown_json,wc.metadata_json", params)
        digest = hashlib.sha256()
        ids: set[tuple[str, int]] = set()
        count = 0
        for row in rows:
            digest.update(json.dumps(tuple(row), sort_keys=True, default=str).encode("utf-8"))
            digest.update(b"\n")
            count += 1
            ids.add((str(row["source"]), int(row["chart_id"])))
        return count, digest.hexdigest(), ids

    def assign_current_progress_candidates_to_task(self, workspace_id: int | str, task_id: int) -> int:
        """Attach only exact relationships covered by current song progress."""
        db_id = self._workspace_db_id(workspace_id)
        with self._conn:
            cur = self._conn.execute(
                """UPDATE workspace_candidates SET task_id = ?
                   WHERE id IN (
                     SELECT wc.id FROM workspace_candidates wc
                     JOIN spotify_tracks t ON t.id = wc.track_id
                     JOIN discovery_song_progress dsp
                       ON dsp.workspace_id = wc.workspace_id AND dsp.track_key = t.track_key
                     WHERE wc.workspace_id = ? AND wc.kind = 'exact'
                   )""",
                (int(task_id), db_id),
            )
        return int(cur.rowcount)

    def get_matching_song_cache(self, fingerprint: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM matching_song_cache WHERE fingerprint = ?", (str(fingerprint),)
        ).fetchone()

    def get_matching_scan_cache(self, workspace_id: int | str, fingerprint: str) -> sqlite3.Row | None:
        db_id = self._workspace_db_id(workspace_id)
        return self._conn.execute("SELECT * FROM matching_scan_cache WHERE workspace_id=? AND fingerprint=?", (db_id, str(fingerprint))).fetchone()

    def save_matching_scan_cache(self, workspace_id: int | str, fingerprint: str, track_count: int, result: Mapping[str, Any]) -> None:
        db_id = self._workspace_db_id(workspace_id)
        now = time.time()
        with self._conn:
            revision = self._conn.execute("SELECT candidate_revision FROM discovery_workspaces WHERE id=?", (db_id,)).fetchone()["candidate_revision"]
            self._conn.execute("""INSERT INTO matching_scan_cache(workspace_id,fingerprint,result_json,track_count,created_at,updated_at,candidate_revision)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(workspace_id) DO UPDATE SET fingerprint=excluded.fingerprint,result_json=excluded.result_json,track_count=excluded.track_count,updated_at=excluded.updated_at,candidate_revision=excluded.candidate_revision""", (db_id, str(fingerprint), json.dumps(dict(result), sort_keys=True, default=str), int(track_count), now, now, revision))

    def get_workspace_candidate_revision(self, workspace_id: int | str) -> int:
        db_id = self._workspace_db_id(workspace_id)
        row = self._conn.execute("SELECT candidate_revision FROM discovery_workspaces WHERE id=?", (db_id,)).fetchone()
        return int(row["candidate_revision"] or 0) if row else 0

    def update_matching_scan_cache_revision(self, workspace_id: int | str, revision: int) -> None:
        db_id = self._workspace_db_id(workspace_id)
        with self._conn:
            self._conn.execute("UPDATE matching_scan_cache SET candidate_revision=?, updated_at=? WHERE workspace_id=?", (int(revision), time.time(), db_id))

    def save_matching_song_cache(
        self, fingerprint: str, track_key: str, classification: str, result: Mapping[str, Any]
    ) -> None:
        now = time.time()
        with self._conn:
            self._conn.execute(
                """INSERT INTO matching_song_cache(fingerprint,track_key,classification,result_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(fingerprint) DO UPDATE SET
                   track_key=excluded.track_key,classification=excluded.classification,
                   result_json=excluded.result_json,updated_at=excluded.updated_at""",
                (str(fingerprint), str(track_key), str(classification), json.dumps(dict(result), sort_keys=True, default=str), now, now),
            )

    def update_workspace_expansion_artist_metadata(
        self,
        workspace_id: int | str,
        artist_norm: str,
        metadata: Mapping[str, Any],
    ) -> None:
        """Update one artist preview without replacing the workspace artist list."""
        db_id = self._workspace_db_id(workspace_id)
        with self._conn:
            self._conn.execute(
                """UPDATE workspace_expansion_artists SET metadata_json = ?
                   WHERE workspace_id = ? AND artist_norm = ?""",
                (_json_text(metadata, {}), db_id, artist_norm),
            )

    def set_expansion_artist_selected(
        self,
        workspace_id: int | str,
        artist_norm: str,
        selected: bool = True,
    ) -> None:
        db_id = self._workspace_db_id(workspace_id)
        with self._conn:
            self._conn.execute(
                """UPDATE workspace_expansion_artists SET selected = ?
                   WHERE workspace_id = ? AND artist_norm = ?""",
                (int(bool(selected)), db_id, artist_norm),
            )

    select_expansion_artist = set_expansion_artist_selected

    def get_workspace_expansion_artists(
        self,
        workspace_id: int | str,
        *,
        selected_only: bool = False,
    ) -> list[sqlite3.Row]:
        db_id = self._workspace_db_id(workspace_id)
        sql = "SELECT * FROM workspace_expansion_artists WHERE workspace_id = ?"
        params: list[Any] = [db_id]
        if selected_only:
            sql += " AND selected = 1"
        sql += " ORDER BY artist_name COLLATE NOCASE"
        return list(self._conn.execute(sql, params))

    list_expansion_artists = get_workspace_expansion_artists

    # ---------------------------------------------- durable expansion batches
    def create_expansion_batch(
        self,
        workspace_id: int | str,
        artists: Iterable[Mapping[str, Any] | Any],
        *,
        run_id: str = "",
        selection_fingerprint: str = "",
        status: str = "pending",
        phase: str = "preview",
        metadata: Mapping[str, Any] | None = None,
    ) -> int:
        """Create a durable, ordered batch for at most 25 existing artists."""
        db_id = self._workspace_db_id(workspace_id)
        values = list(artists)
        if len(values) > 25:
            raise ValueError("artist expansion batches may contain at most 25 artists")
        resolved: list[sqlite3.Row] = []
        seen: set[str] = set()
        for value in values:
            data = _as_mapping(value)
            if not data and isinstance(value, str):
                data = {"artist_norm": value}
            norm = str(_first_value(data, "artist_norm", "normalized", default="") or "")
            if not norm or norm in seen:
                continue
            row = self._conn.execute(
                """SELECT * FROM workspace_expansion_artists
                   WHERE workspace_id = ? AND artist_norm = ?""",
                (db_id, norm),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown workspace expansion artist: {norm}")
            seen.add(norm)
            resolved.append(row)
        if len(resolved) > 25:
            raise ValueError("artist expansion batches may contain at most 25 artists")
        actual_run_id = run_id.strip() or uuid.uuid4().hex
        fingerprint = selection_fingerprint.strip() or hashlib.sha256(
            "\n".join(sorted(seen)).encode("utf-8")
        ).hexdigest()
        now = time.time()
        started_at = now if status in {"running", "previewing", "searching"} else None
        with self._conn:
            cur = self._conn.execute(
                """INSERT INTO workspace_expansion_batches
                   (workspace_id, run_id, status, phase, selection_fingerprint,
                    total_artists, heartbeat_at, created_at, started_at, updated_at,
                    metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    db_id, actual_run_id, status, phase, fingerprint, len(resolved),
                    now, now, started_at, now, _json_text(metadata or {}, {}),
                ),
            )
            batch_id = int(cur.lastrowid)
            self._conn.executemany(
                """INSERT INTO workspace_expansion_batch_artists
                   (batch_id, expansion_artist_id, artist_name, artist_norm,
                    ordinal, updated_at) VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    (batch_id, row["id"], row["artist_name"], row["artist_norm"], ordinal, now)
                    for ordinal, row in enumerate(resolved)
                ],
            )
        return batch_id

    def get_expansion_batch(
        self,
        batch_id: int | None = None,
        *,
        workspace_id: int | str | None = None,
        run_id: str = "",
    ) -> sqlite3.Row | None:
        """Load a batch by id, workspace/run id, or the workspace's latest batch."""
        if batch_id is not None:
            return self._conn.execute(
                "SELECT * FROM workspace_expansion_batches WHERE id = ?", (batch_id,)
            ).fetchone()
        if workspace_id is None:
            raise ValueError("batch_id or workspace_id is required")
        db_id = self._workspace_db_id(workspace_id)
        if run_id:
            return self._conn.execute(
                """SELECT * FROM workspace_expansion_batches
                   WHERE workspace_id = ? AND run_id = ?""",
                (db_id, run_id),
            ).fetchone()
        return self._conn.execute(
            """SELECT * FROM workspace_expansion_batches
               WHERE workspace_id = ? ORDER BY id DESC LIMIT 1""",
            (db_id,),
        ).fetchone()

    def initialize_discovery_song_progress(
        self, workspace_id: int | str, tracks: Iterable[Any]
    ) -> None:
        """Create compact activity rows without storing candidate payloads."""
        db_id = self._workspace_db_id(workspace_id)
        now = time.time()
        rows = [
            (db_id, str(track.track_key), str(track.artist_full), str(track.title), now)
            for track in tracks
        ]
        with self._conn:
            self._conn.execute(
                "DELETE FROM discovery_song_progress WHERE workspace_id = ?", (db_id,)
            )
            self._conn.executemany(
                """INSERT INTO discovery_song_progress
                   (workspace_id,track_key,artist,title,status,updated_at)
                   VALUES (?,?,?,?, 'waiting', ?)""",
                rows,
            )

    def update_discovery_song_progress(
        self, workspace_id: int | str, track_key: str, **values: Any
    ) -> None:
        db_id = self._workspace_db_id(workspace_id)
        allowed = {"status", "candidate_count", "provenance", "classification", "reason"}
        assignments = [f"{key} = ?" for key in values if key in allowed]
        params = [values[key] for key in values if key in allowed]
        assignments.append("updated_at = ?")
        params.extend([time.time(), db_id, str(track_key)])
        with self._conn:
            self._conn.execute(
                "UPDATE discovery_song_progress SET " + ", ".join(assignments)
                + " WHERE workspace_id = ? AND track_key = ?",
                params,
            )

    def update_discovery_song_progress_batch(
        self, workspace_id: int | str, rows: Iterable[tuple[str, Mapping[str, Any]]]
    ) -> None:
        """Apply many song classifications in one transaction."""
        db_id = self._workspace_db_id(workspace_id)
        now = time.time()
        with self._conn:
            for track_key, values in rows:
                assignments = [f"{key} = ?" for key in values if key in {"status", "candidate_count", "provenance", "classification", "reason"}]
                params = [values[key] for key in values if key in {"status", "candidate_count", "provenance", "classification", "reason"}]
                assignments.append("updated_at = ?")
                params.extend([now, db_id, str(track_key)])
                self._conn.execute("UPDATE discovery_song_progress SET " + ", ".join(assignments) + " WHERE workspace_id = ? AND track_key = ?", params)

    def get_discovery_song_progress(
        self, workspace_id: int | str, *, limit: int = 100, offset: int = 0,
        search: str = ""
    ) -> list[sqlite3.Row]:
        db_id = self._workspace_db_id(workspace_id)
        limit = min(200, max(1, int(limit)))
        offset = max(0, int(offset))
        query = "SELECT * FROM discovery_song_progress WHERE workspace_id = ?"
        params: list[Any] = [db_id]
        if search.strip():
            query += " AND (artist LIKE ? OR title LIKE ?)"
            value = f"%{search.strip()}%"
            params.extend([value, value])
        query += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return list(self._conn.execute(query, params))

    def get_discovery_song_progress_summary(self, workspace_id: int | str) -> dict[str, Any]:
        """Return compact current-run classification totals for the UI."""
        db_id = self._workspace_db_id(workspace_id)
        row = self._conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN status='complete' THEN 1 ELSE 0 END) AS completed,
                      SUM(CASE WHEN status='checking' THEN 1 ELSE 0 END) AS checking,
                      SUM(CASE WHEN classification='strong' THEN 1 ELSE 0 END) AS strong,
                      SUM(CASE WHEN classification='review' THEN 1 ELSE 0 END) AS review,
                      SUM(CASE WHEN classification='no_suitable' THEN 1 ELSE 0 END) AS no_suitable,
                      SUM(CASE WHEN classification='not_playable' THEN 1 ELSE 0 END) AS not_playable,
                      SUM(CASE WHEN classification='no_catalog_candidate' THEN 1 ELSE 0 END) AS no_catalog_candidate
                 FROM discovery_song_progress WHERE workspace_id=?""",
            (db_id,),
        ).fetchone()
        return {key: int(row[key] or 0) for key in ("total", "completed", "checking", "strong", "review", "no_suitable", "not_playable", "no_catalog_candidate")}

    def get_discovery_song_yield_summary(self, workspace_id: int | str) -> dict[str, Any]:
        """Aggregate terminal song classes and reasons without a page-size cap."""
        db_id = self._workspace_db_id(workspace_id)
        rows = self._conn.execute(
            """SELECT classification,reason,COUNT(*) AS count
               FROM discovery_song_progress
               WHERE workspace_id=? AND status='complete'
               GROUP BY classification,reason""",
            (db_id,),
        )
        classifications: dict[str, int] = {}
        reasons: dict[str, int] = {}
        total = 0
        for row in rows:
            count = int(row["count"] or 0)
            classification = str(row["classification"] or "")
            reason = str(row["reason"] or "")
            classifications[classification] = classifications.get(classification, 0) + count
            if reason:
                reasons[reason] = reasons.get(reason, 0) + count
            total += count
        return {"total": total, "classifications": classifications, "reasons": reasons}

    def count_discovery_song_progress(
        self, workspace_id: int | str, *, search: str = ""
    ) -> int:
        db_id = self._workspace_db_id(workspace_id)
        query = "SELECT COUNT(*) AS count FROM discovery_song_progress WHERE workspace_id = ?"
        params: list[Any] = [db_id]
        if search.strip():
            query += " AND (artist LIKE ? OR title LIKE ?)"
            value = f"%{search.strip()}%"
            params.extend([value, value])
        return int(self._conn.execute(query, params).fetchone()["count"])

    load_expansion_batch = get_expansion_batch

    def get_expansion_batch_artists(self, batch_id: int) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                """SELECT * FROM workspace_expansion_batch_artists
                   WHERE batch_id = ? ORDER BY ordinal""",
                (batch_id,),
            )
        )

    list_expansion_batch_artists = get_expansion_batch_artists

    def request_expansion_batch_cancel(self, batch_id: int) -> sqlite3.Row:
        now = time.time()
        with self._conn:
            self._conn.execute(
                """UPDATE workspace_expansion_batches
                   SET cancel_requested = 1, heartbeat_at = ?, updated_at = ?
                   WHERE id = ?""",
                (now, now, batch_id),
            )
        row = self.get_expansion_batch(batch_id)
        if row is None:
            raise ValueError(f"unknown expansion batch: {batch_id}")
        return row

    cancel_expansion_batch = request_expansion_batch_cancel

    def resume_expansion_batch(
        self,
        batch_id: int,
        *,
        run_id: str = "",
        phase: str | None = None,
        retry_failed: bool = False,
    ) -> sqlite3.Row:
        """Assign a new run id and continue only unfinished artist phases."""
        row = self.get_expansion_batch(batch_id)
        if row is None:
            raise ValueError(f"unknown expansion batch: {batch_id}")
        now = time.time()
        statuses = ["running", "interrupted"]
        if retry_failed:
            statuses.append("failed")
        placeholders = ",".join("?" for _ in statuses)
        with self._conn:
            for column in ("preview_status", "search_status"):
                self._conn.execute(
                    f"""UPDATE workspace_expansion_batch_artists
                        SET {column} = 'pending', error = NULL, completed_at = NULL,
                            updated_at = ?
                        WHERE batch_id = ? AND {column} IN ({placeholders})""",
                    (now, batch_id, *statuses),
                )
            self._conn.execute(
                """UPDATE workspace_expansion_batches
                   SET run_id = ?, status = 'running', phase = ?, cancel_requested = 0,
                       current_artist = '', heartbeat_at = ?, started_at = ?,
                       completed_at = NULL, updated_at = ?, error = NULL
                   WHERE id = ?""",
                (run_id.strip() or uuid.uuid4().hex, phase or row["phase"], now, now, now, batch_id),
            )
            aggregate_phase = "search" if "search" in str(phase or row["phase"]) else "preview"
            self._refresh_expansion_batch_totals(batch_id, aggregate_phase, now)
        resumed = self.get_expansion_batch(batch_id)
        assert resumed is not None
        return resumed

    def update_expansion_batch(
        self,
        batch_id: int,
        updates: Mapping[str, Any] | None = None,
        **values: Any,
    ) -> sqlite3.Row:
        """Checkpoint batch progress and refresh its heartbeat."""
        data = dict(updates or {})
        data.update(values)
        allowed = {
            "status", "phase", "cancel_requested", "completed_artists",
            "failed_artists", "skipped_artists", "network_requests",
            "current_artist", "error", "metadata_json", "completed_at",
        }
        terminal = {"complete", "completed", "partial", "failed", "cancelled", "interrupted"}
        assignments: list[str] = []
        params: list[Any] = []
        if "metadata" in data and "metadata_json" not in data:
            data["metadata_json"] = data.pop("metadata")
        for key, value in data.items():
            if key not in allowed:
                continue
            if key == "metadata_json" and not isinstance(value, str):
                value = _json_text(value, {})
            if key == "cancel_requested":
                value = int(bool(value))
            assignments.append(f"{key} = ?")
            params.append(value)
        now = time.time()
        if str(data.get("status") or "").lower() in terminal and "completed_at" not in data:
            assignments.append("completed_at = ?")
            params.append(now)
        assignments.extend(["heartbeat_at = ?", "updated_at = ?"])
        params.extend([now, now, batch_id])
        with self._conn:
            cur = self._conn.execute(
                "UPDATE workspace_expansion_batches SET " + ", ".join(assignments) + " WHERE id = ?",
                params,
            )
        if cur.rowcount == 0:
            raise ValueError(f"unknown expansion batch: {batch_id}")
        row = self.get_expansion_batch(batch_id)
        assert row is not None
        return row

    update_expansion_batch_progress = update_expansion_batch
    heartbeat_expansion_batch = update_expansion_batch

    def checkpoint_expansion_batch_artist(
        self,
        batch_id: int,
        artist_norm: str,
        *,
        phase: str,
        status: str,
        request_count: int | None = None,
        error: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> sqlite3.Row:
        """Persist one preview/search boundary and refresh aggregate progress."""
        if phase not in {"preview", "search"}:
            raise ValueError("expansion artist phase must be 'preview' or 'search'")
        status_column = f"{phase}_status"
        requests_column = f"{phase}_requests"
        terminal = status in {"complete", "completed", "success", "failed", "skipped", "cancelled"}
        now = time.time()
        assignments = [f"{status_column} = ?", "error = ?", "updated_at = ?"]
        params: list[Any] = [status, error, now]
        if request_count is not None:
            assignments.append(f"{requests_column} = ?")
            params.append(max(0, int(request_count)))
        if metadata is not None:
            assignments.append("metadata_json = ?")
            params.append(_json_text(metadata, {}))
        if status == "running":
            assignments.append("started_at = COALESCE(started_at, ?)")
            params.append(now)
        if terminal:
            assignments.append("completed_at = ?")
            params.append(now)
        params.extend([batch_id, artist_norm])
        with self._conn:
            cur = self._conn.execute(
                "UPDATE workspace_expansion_batch_artists SET " + ", ".join(assignments)
                + " WHERE batch_id = ? AND artist_norm = ?",
                params,
            )
            if cur.rowcount == 0:
                raise ValueError(f"unknown expansion batch artist: {artist_norm}")
            self._refresh_expansion_batch_totals(batch_id, phase, now)
        row = self._conn.execute(
            """SELECT * FROM workspace_expansion_batch_artists
               WHERE batch_id = ? AND artist_norm = ?""",
            (batch_id, artist_norm),
        ).fetchone()
        assert row is not None
        return row

    def _refresh_expansion_batch_totals(self, batch_id: int, phase: str, now: float) -> None:
        column = "preview_status" if phase == "preview" else "search_status"
        counts = self._conn.execute(
            f"""SELECT
                  SUM(CASE WHEN {column} IN ('complete', 'completed', 'success') THEN 1 ELSE 0 END) AS completed,
                  SUM(CASE WHEN {column} = 'failed' THEN 1 ELSE 0 END) AS failed,
                  SUM(CASE WHEN {column} = 'skipped' THEN 1 ELSE 0 END) AS skipped,
                  SUM(preview_requests + search_requests) AS requests
                FROM workspace_expansion_batch_artists WHERE batch_id = ?""",
            (batch_id,),
        ).fetchone()
        self._conn.execute(
            """UPDATE workspace_expansion_batches SET completed_artists = ?,
               failed_artists = ?, skipped_artists = ?, network_requests = ?,
               heartbeat_at = ?, updated_at = ? WHERE id = ?""",
            (
                int(counts["completed"] or 0), int(counts["failed"] or 0),
                int(counts["skipped"] or 0), int(counts["requests"] or 0),
                now, now, batch_id,
            ),
        )

    def mark_active_expansion_batches_interrupted(self) -> int:
        """Mark durable work left active by a previous process as interrupted."""
        now = time.time()
        active = ("created", "pending", "queued", "starting", "running", "previewing", "searching")
        placeholders = ",".join("?" for _ in active)
        with self._conn:
            rows = list(
                self._conn.execute(
                    f"SELECT id FROM workspace_expansion_batches WHERE status IN ({placeholders})",
                    active,
                )
            )
            ids = [int(row["id"]) for row in rows]
            for batch_id in ids:
                self._conn.execute(
                    """UPDATE workspace_expansion_batch_artists SET
                       preview_status = CASE WHEN preview_status = 'running' THEN 'interrupted' ELSE preview_status END,
                       search_status = CASE WHEN search_status = 'running' THEN 'interrupted' ELSE search_status END,
                       updated_at = ? WHERE batch_id = ?""",
                    (now, batch_id),
                )
                self._conn.execute(
                    """UPDATE workspace_expansion_batches SET status = 'interrupted',
                       completed_at = ?, heartbeat_at = ?, updated_at = ? WHERE id = ?""",
                    (now, now, now, batch_id),
                )
        return len(ids)

    interrupt_active_expansion_batches = mark_active_expansion_batches_interrupted

    def replace_expansion_artist_candidates(
        self,
        workspace_id: int | str,
        artist_norm: str,
        candidates: Iterable[Any],
        *,
        batch_id: int | None = None,
        request_count: int | None = None,
    ) -> list[int]:
        """Atomically replace one artist's expansion rows and stale selections."""
        db_id = self._workspace_db_id(workspace_id)
        prepared: list[dict[str, Any]] = []
        seen: set[tuple[int | None, int]] = set()
        for value in candidates:
            data = _as_mapping(value)
            candidate = data.get("candidate") or data.get("chart") or data.get("candidate_id") or value
            candidate_id = self._candidate_db_id(candidate)
            track_value = data.get("track_id") or data.get("spotify_track_id") or data.get("track")
            track_id = self._resolve_track_value(track_value) if track_value is not None else None
            identity = (track_id, candidate_id)
            if identity in seen:
                continue
            seen.add(identity)
            breakdown = data.get("breakdown_json", data.get("breakdown", {}))
            score = float(data.get("score", data.get("final_score", 0)) or 0)
            prepared.append({
                "candidate_id": candidate_id,
                "track_id": track_id,
                "task_id": data.get("task_id"),
                "score": score,
                "quality": float(data.get("quality", self._candidate_quality(candidate, breakdown)) or 0),
                "decision": str(data.get("decision") or ""),
                "eligibility": str(data.get("eligibility") or ""),
                "breakdown_json": breakdown if isinstance(breakdown, str) else _json_text(breakdown, {}),
                "metadata_json": _json_text(data.get("metadata", {}), {}),
            })
        now = time.time()
        ids: list[int] = []
        with self._conn:
            self._conn.execute(
                """DELETE FROM workspace_selections
                   WHERE workspace_id = ? AND kind = 'expansion' AND artist_norm = ?""",
                (db_id, artist_norm),
            )
            self._conn.execute(
                """DELETE FROM workspace_candidates
                   WHERE workspace_id = ? AND kind = 'expansion' AND artist_norm = ?""",
                (db_id, artist_norm),
            )
            for item in prepared:
                cur = self._conn.execute(
                    """INSERT INTO workspace_candidates
                       (workspace_id, track_id, candidate_id, kind, artist_norm,
                        task_id, score, quality, decision, eligibility,
                        breakdown_json, metadata_json)
                       VALUES (?, ?, ?, 'expansion', ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        db_id, item["track_id"], item["candidate_id"], artist_norm,
                        item["task_id"], item["score"], item["quality"],
                        item["decision"], item["eligibility"],
                        item["breakdown_json"] or "{}", item["metadata_json"],
                    ),
                )
                ids.append(int(cur.lastrowid))
            if batch_id is not None:
                cur = self._conn.execute(
                    """UPDATE workspace_expansion_batch_artists SET search_status = 'complete',
                       search_requests = COALESCE(?, search_requests), error = NULL,
                       completed_at = ?, updated_at = ?
                       WHERE batch_id = ? AND artist_norm = ?""",
                    (request_count, now, now, batch_id, artist_norm),
                )
                if cur.rowcount == 0:
                    raise ValueError(f"unknown expansion batch artist: {artist_norm}")
                self._refresh_expansion_batch_totals(batch_id, "search", now)
        return ids

    replace_workspace_expansion_artist_candidates = replace_expansion_artist_candidates

    def clear_workspace_expansion_state(self, workspace_id: int | str) -> None:
        """Clear expansion-only work while preserving exact discovery state."""
        db_id = self._workspace_db_id(workspace_id)
        rows = list(
            self._conn.execute(
                """SELECT id, metadata_json FROM workspace_expansion_artists
                   WHERE workspace_id = ?""",
                (db_id,),
            )
        )
        with self._conn:
            self._conn.execute(
                "DELETE FROM workspace_selections WHERE workspace_id = ? AND kind = 'expansion'",
                (db_id,),
            )
            self._conn.execute(
                "DELETE FROM workspace_candidates WHERE workspace_id = ? AND kind = 'expansion'",
                (db_id,),
            )
            self._conn.execute(
                "DELETE FROM workspace_expansion_batches WHERE workspace_id = ?",
                (db_id,),
            )
            for row in rows:
                try:
                    metadata = json.loads(str(row["metadata_json"] or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    metadata = {}
                if not isinstance(metadata, dict):
                    metadata = {}
                metadata.pop("discography_preview", None)
                self._conn.execute(
                    """UPDATE workspace_expansion_artists
                       SET selected = 0, metadata_json = ? WHERE id = ?""",
                    (_json_text(metadata, {}), int(row["id"])),
                )

    def select_expansion_candidate(
        self,
        workspace_id: int | str,
        artist_norm: str,
        candidate: Any,
        **values: Any,
    ) -> int:
        return self.select_workspace_candidate(
            workspace_id,
            values.pop("track_id", None),
            candidate,
            kind="expansion",
            artist_norm=artist_norm,
            **values,
        )

    def clear_expansion_selection(
        self,
        workspace_id: int | str,
        artist_norm: str = "",
        candidate_id: int | None = None,
    ) -> int:
        return self.clear_workspace_selection(
            workspace_id,
            candidate_id=candidate_id,
            kind="expansion",
            artist_norm=artist_norm,
        )

    def list_expansion_selections(self, workspace_id: int | str) -> list[sqlite3.Row]:
        return self.list_workspace_selections(workspace_id, kind="expansion")

    get_expansion_selections = list_expansion_selections

    # ------------------------------------------------------------- library UI
    def get_spotify_tracks(self, include_ignored: bool = False) -> list[Track]:
        tracks = self.get_tracks(include_ignored=include_ignored)
        result: list[Track] = []
        for track in tracks:
            if not track.spotify_id:
                continue
            names = list(track.playlists)
            names.extend(
                item["name"]
                for item in self._conn.execute(
                    """SELECT p.name FROM spotify_playlist_tracks pt
                       JOIN spotify_playlists p ON p.id = pt.playlist_id
                       WHERE pt.track_id = ? ORDER BY pt.position, p.name""",
                    (track.db_id,),
                )
            )
            if self._conn.execute(
                "SELECT 1 FROM spotify_liked_tracks WHERE track_id = ?", (track.db_id,)
            ).fetchone():
                names.insert(0, "Liked Songs")
            if names:
                track.playlists = list(dict.fromkeys([*track.playlists, *names]))
            result.append(track)
        return result

    def get_spotify_track(self, spotify_id: str) -> Track | None:
        return next(
            (track for track in self.get_spotify_tracks(True) if track.spotify_id == str(spotify_id)),
            None,
        )

    def get_library_track_rows(
        self,
        folder_id: str | int | None = None,
        *,
        liked: bool = False,
        workspace_id: int | str | None = None,
        include_ignored: bool = False,
        playlist_id: str | int | None = None,
    ) -> list[sqlite3.Row]:
        if folder_id is None:
            folder_id = playlist_id
        clauses = ["t.spotify_id IS NOT NULL", "t.spotify_id != ''"]
        if not include_ignored:
            clauses.append("t.ignored = 0")
        params: list[Any] = []
        if liked or str(folder_id or "").casefold() in {"liked", "liked songs", "liked_songs"}:
            clauses.append("EXISTS (SELECT 1 FROM spotify_liked_tracks l WHERE l.track_id = t.id)")
        elif folder_id is not None:
            playlist = self.get_spotify_playlist(folder_id)
            if playlist is None:
                return []
            clauses.append(
                "EXISTS (SELECT 1 FROM spotify_playlist_tracks pt "
                "WHERE pt.track_id = t.id AND pt.playlist_id = ?)"
            )
            params.append(playlist["id"])
        db_id: int | None = None
        if workspace_id is not None:
            db_id = self._workspace_db_id(workspace_id)
            clauses.append(
                f"EXISTS (SELECT 1 FROM workspace_library_tracks wlt WHERE wlt.workspace_id = {db_id} AND wlt.track_id = t.id)"
            )
        workspace_candidate_filter = f" AND wc2.workspace_id = {db_id}" if db_id is not None else ""
        workspace_selection_filter = f" AND ws2.workspace_id = {db_id}" if db_id is not None else ""
        sql = (
            "SELECT t.*, "
            "(SELECT COUNT(*) FROM workspace_candidates wc2 WHERE wc2.track_id = t.id"
            + workspace_candidate_filter
            + ") AS candidate_count, "
            "(SELECT COUNT(*) FROM workspace_selections ws2 WHERE ws2.track_id = t.id "
            "AND ws2.status = 'selected'"
            + workspace_selection_filter
            + ") AS selected_count, "
            + (f"(SELECT already_owned FROM workspace_library_tracks wlt2 WHERE wlt2.workspace_id = {db_id} AND wlt2.track_id = t.id) AS already_owned, "
               f"(SELECT owned_count FROM workspace_library_tracks wlt3 WHERE wlt3.workspace_id = {db_id} AND wlt3.track_id = t.id) AS owned_count "
               if db_id is not None else "0 AS already_owned, 0 AS owned_count ")
             + "FROM spotify_tracks t"
            + " WHERE " + " AND ".join(clauses)
            + " ORDER BY t.primary_artist COLLATE NOCASE, t.title COLLATE NOCASE"
        )
        return list(self._conn.execute(sql, params))

    def get_library_tracks(
        self,
        folder_id: str | int | None = None,
        *,
        liked: bool = False,
        include_ignored: bool = False,
        workspace_id: int | str | None = None,
        playlist_id: str | int | None = None,
    ) -> list[Track]:
        if folder_id is None:
            folder_id = playlist_id
        rows = self.get_library_track_rows(
            folder_id,
            liked=liked,
            workspace_id=workspace_id,
            include_ignored=include_ignored,
        )
        # Load the full library once; reloading it per row made this O(n^2)
        # and froze large workspaces for minutes.
        tracks_by_id = {item.db_id: item for item in self.get_spotify_tracks(True)}
        tracks: list[Track] = []
        for row in rows:
            track = tracks_by_id.get(row["id"])
            if track is None:
                continue
            names = list(track.playlists)
            names.extend(
                item["name"]
                for item in self._conn.execute(
                    """SELECT p.name FROM spotify_playlist_tracks pt
                       JOIN spotify_playlists p ON p.id = pt.playlist_id
                       WHERE pt.track_id = ? ORDER BY pt.position, p.name""",
                    (track.db_id,),
                )
            )
            if self._conn.execute(
                "SELECT 1 FROM spotify_liked_tracks WHERE track_id = ?", (track.db_id,)
            ).fetchone():
                names.insert(0, "Liked Songs")
            track.playlists = list(dict.fromkeys(names))
            tracks.append(track)
        return tracks

    def get_liked_tracks(self, **kwargs: Any) -> list[Track]:
        return self.get_library_tracks(liked=True, **kwargs)

    def get_playlist_tracks(self, playlist_id: str | int, **kwargs: Any) -> list[Track]:
        return self.get_library_tracks(playlist_id, **kwargs)

    get_folder_tracks = get_library_tracks

    def get_spotify_playlist_memberships(
        self,
        playlist_id: str | int | None = None,
    ) -> list[sqlite3.Row]:
        if playlist_id is None:
            return list(self._conn.execute("SELECT * FROM spotify_playlist_tracks ORDER BY playlist_id, position, id"))
        playlist = self.get_spotify_playlist(playlist_id)
        if playlist is None:
            return []
        return list(
            self._conn.execute(
                "SELECT * FROM spotify_playlist_tracks WHERE playlist_id = ? ORDER BY position, id",
                (playlist["id"],),
            )
        )

    get_playlist_memberships = get_spotify_playlist_memberships

    def get_spotify_liked_tracks(self) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM spotify_liked_tracks ORDER BY added_at, spotify_track_id"
            )
        )

    def get_library_folders(
        self,
        *,
        workspace_id: int | str | None = None,
        include_inaccessible: bool = True,
    ) -> list[dict[str, Any]]:
        folders: list[dict[str, Any]] = []
        workspace_db_id = self._workspace_db_id(workspace_id) if workspace_id is not None else None
        workspace_tracks = (
            self.get_workspace_library_track_ids(workspace_db_id)
            if workspace_db_id is not None
            else None
        )
        candidate_tracks = {
            int(row[0]) for row in self._conn.execute(
                "SELECT DISTINCT track_id FROM workspace_candidates WHERE workspace_id = ? AND track_id IS NOT NULL",
                (workspace_db_id,),
            )
        } if workspace_db_id is not None else set()
        eligible_tracks = {
            int(row[0]) for row in self._conn.execute(
                """SELECT DISTINCT track_id FROM workspace_candidates
                   WHERE workspace_id = ? AND track_id IS NOT NULL
                     AND eligibility IN ('RECOMMENDED', 'ELIGIBLE_REVIEW')""",
                (workspace_db_id,),
            )
        } if workspace_db_id is not None else set()

        def match_counts(track_ids: set[int]) -> dict[str, int]:
            total_count = len(track_ids)
            if workspace_db_id is None:
                return {"candidate_count": 0, "eligible_count": 0, "no_match_count": 0}
            candidate_count = len(track_ids & candidate_tracks)
            eligible_count = len(track_ids & eligible_tracks)
            return {
                "candidate_count": candidate_count,
                "eligible_count": eligible_count,
                "no_match_count": max(0, total_count - candidate_count),
            }

        liked_tracks = {
            int(row[0]) for row in self._conn.execute(
                """SELECT l.track_id FROM spotify_liked_tracks l
                   JOIN spotify_tracks t ON t.id = l.track_id WHERE t.ignored = 0"""
            )
        }
        if workspace_tracks is not None:
            liked_tracks &= workspace_tracks
        liked_count = len(liked_tracks)
        liked_stats = match_counts(liked_tracks)
        folders.append(
            {
                "id": "liked",
                "spotify_id": "liked",
                "name": "Liked Songs",
                "url": "",
                "artwork_url": "",
                "access_status": "accessible",
                "is_liked": True,
                "track_count": liked_count,
                **liked_stats,
            }
        )
        playlists = self.get_spotify_playlists(include_inaccessible=include_inaccessible)
        playlist_tracks: dict[int, set[int]] = {}
        for row in self._conn.execute(
            """SELECT pt.playlist_id, pt.track_id FROM spotify_playlist_tracks pt
               JOIN spotify_tracks t ON t.id = pt.track_id WHERE t.ignored = 0"""
        ):
            track_id = int(row["track_id"])
            if workspace_tracks is None or track_id in workspace_tracks:
                playlist_tracks.setdefault(int(row["playlist_id"]), set()).add(track_id)
        for playlist in playlists:
            track_ids = playlist_tracks.get(int(playlist["id"]), set())
            count = len(track_ids)
            playlist_stats = match_counts(track_ids)
            folder = dict(playlist)
            folder.update(
                {
                    "db_id": playlist["id"],
                    "id": playlist["spotify_id"],
                    "folder_id": playlist["spotify_id"],
                    "is_liked": False,
                    "track_count": count,
                    **playlist_stats,
                }
            )
            folders.append(folder)
        return folders

    get_library_folders_and_tracks = get_library_folders

    # --------------------------------------------------------- export records
    def _export_db_id(self, value: int | str) -> int:
        if isinstance(value, int) and not isinstance(value, bool):
            row = self._conn.execute("SELECT id FROM export_records WHERE id = ?", (value,)).fetchone()
        else:
            row = self._conn.execute(
                "SELECT id FROM export_records WHERE export_key = ? OR id = ?",
                (str(value), int(str(value)) if str(value).isdigit() else -1),
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown export record: {value}")
        return int(row["id"])

    def create_export_record(
        self,
        workspace_id: int | str | None = None,
        export: Mapping[str, Any] | Any | None = None,
        *,
        export_key: str = "",
        kind: str = "folder",
        destination: str = "",
        manifest_path: str = "",
        status: str = "created",
        managed: bool = False,
        manifest: Any | None = None,
        metadata: Mapping[str, Any] | None = None,
        **values: Any,
    ) -> int:
        # Accept create_export_record(export=...) and the common positional
        # create_export_record(workspace_id, export) forms.
        data = _as_mapping(export)
        data.update(values)
        if isinstance(workspace_id, Mapping) or (
            workspace_id is not None and not isinstance(workspace_id, (int, str))
        ):
            if export is None:
                export = workspace_id
                data = _as_mapping(export)
                workspace_id = data.get("workspace_id")
            else:
                workspace_id, export = export, workspace_id
                data = _as_mapping(export)
                data.update(values)
        if manifest is None:
            manifest = data.get("manifest")
        key = str(_first_value(data, "export_key", "run_id", "id", default=export_key) or "")
        if not key:
            key = uuid.uuid4().hex
        workspace_db_id = self._workspace_db_id(workspace_id) if workspace_id is not None else None
        actual_kind = str(_first_value(data, "kind", "mode", default=kind) or kind)
        actual_destination = str(
            _first_value(data, "destination", "destination_root", "output_path", default=destination)
            or ""
        )
        actual_manifest_path = str(
            _first_value(data, "manifest_path", "manifest_file", default=manifest_path) or ""
        )
        actual_status = str(_first_value(data, "status", default=status) or status)
        actual_managed = bool(_first_value(data, "managed", "managed_sync", default=managed))
        manifest_data = _as_mapping(manifest)
        if isinstance(manifest, str):
            manifest_data = _load_json(manifest, {})
        now = time.time()
        with self._conn:
            self._conn.execute(
                """INSERT INTO export_records
                   (workspace_id, export_key, kind, destination, manifest_path,
                    status, managed, manifest_json, metadata_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(export_key) DO UPDATE SET
                     workspace_id = excluded.workspace_id,
                     kind = excluded.kind, destination = excluded.destination,
                     manifest_path = excluded.manifest_path, status = excluded.status,
                     managed = excluded.managed, manifest_json = excluded.manifest_json,
                     metadata_json = excluded.metadata_json""",
                (
                    workspace_db_id,
                    key,
                    actual_kind,
                    actual_destination,
                    actual_manifest_path,
                    actual_status,
                    int(actual_managed),
                    _json_text(manifest_data, {}),
                    _json_text(_first_value(data, "metadata", default=metadata or {}), {}),
                    now,
                ),
            )
            row = self._conn.execute(
                "SELECT id FROM export_records WHERE export_key = ?", (key,)
            ).fetchone()
            export_id = int(row["id"])
            if manifest_data:
                self._record_export_manifest_locked(export_id, manifest_data, actual_destination)
                raw_records = manifest_data.get("records", manifest_data.get("items", []))
                if isinstance(raw_records, list):
                    for item in raw_records:
                        self._record_export_item_locked(export_id, item)
        return export_id

    def record_export(
        self,
        export: Mapping[str, Any] | Any | None = None,
        workspace_id: int | str | None = None,
        **values: Any,
    ) -> int:
        if (
            isinstance(export, (int, str))
            and workspace_id is not None
            and not isinstance(workspace_id, (int, str))
        ):
            export, workspace_id = workspace_id, export
        return self.create_export_record(workspace_id, export, **values)

    save_export_record = record_export

    def _record_export_manifest_locked(
        self,
        export_id: int,
        manifest: Mapping[str, Any],
        destination: str = "",
    ) -> int:
        data = dict(manifest)
        records = data.get("records", data.get("items", []))
        cur = self._conn.execute(
            """INSERT INTO export_manifests
               (export_id, app, format, version, destination, generated_at, manifest_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(export_id) DO UPDATE SET
                 app = excluded.app, format = excluded.format, version = excluded.version,
                 destination = excluded.destination, generated_at = excluded.generated_at,
                 manifest_json = excluded.manifest_json""",
            (
                export_id,
                str(data.get("app") or "csv-to-clonehero"),
                str(data.get("format") or "csv-to-clonehero-manifest"),
                int(data.get("version", 1) or 1),
                str(data.get("destination") or destination or ""),
                str(data.get("generated_at") or ""),
                _json_text(data, {}),
                time.time(),
            ),
        )
        row = self._conn.execute(
            "SELECT id FROM export_manifests WHERE export_id = ?", (export_id,)
        ).fetchone()
        return int(row["id"] if row else cur.lastrowid)

    def record_export_manifest(
        self,
        export_id: int | str,
        manifest: Mapping[str, Any] | Any,
        *,
        destination: str = "",
    ) -> int:
        data = _as_mapping(manifest)
        if isinstance(manifest, str):
            data = _load_json(manifest, {})
        try:
            db_id = self._export_db_id(export_id)
        except ValueError:
            workspace = self.get_workspace(export_id)
            db_id = self.create_export_record(
                workspace_id=export_id if workspace is not None else None,
                manifest=data,
                destination=destination,
                managed=True,
            )
        with self._conn:
            manifest_id = self._record_export_manifest_locked(db_id, data, destination)
            self._conn.execute(
                "UPDATE export_records SET manifest_json = ?, manifest_path = COALESCE(manifest_path, ?) WHERE id = ?",
                (_json_text(data, {}), destination or None, db_id),
            )
            for item in data.get("records", data.get("items", [])) if isinstance(data, Mapping) else []:
                self._record_export_item_locked(db_id, item)
        return manifest_id

    def _record_export_item_locked(self, export_id: int, item: Any) -> int:
        data = _as_mapping(item)
        export_row = self._conn.execute(
            "SELECT managed FROM export_records WHERE id = ?", (export_id,)
        ).fetchone()
        default_managed = bool(export_row["managed"]) if export_row else True
        chart_id = _first_value(data, "chart_id", "chartId", default="")
        target = str(
            _first_value(data, "target_relpath", "target_path", "path", default="") or ""
        )
        source = str(_first_value(data, "source", default="") or "")
        default_status = "published" if (
            "status" not in data
            and (
                data.get("sha256")
                or data.get("target_relpath")
                or data.get("target_path")
                or data.get("path")
            )
        ) else "pending"
        selection_id = _first_value(data, "selection_id", default=None)
        if selection_id is not None:
            # Selections may be re-built mid-export (e.g. "select all recommended"
            # re-inserts workspace_selections rows).  A stale id must not break
            # export bookkeeping, so drop it instead of failing the FK.
            exists = self._conn.execute(
                "SELECT 1 FROM workspace_selections WHERE id = ?", (selection_id,)
            ).fetchone()
            if exists is None:
                selection_id = None
        self._conn.execute(
            """INSERT INTO export_items
               (export_id, selection_id, source, chart_id, source_hash,
                source_hash_algorithm, artifact_key, target_relpath, path,
                sha256, bytes, status, error, managed, metadata_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(export_id, source, chart_id, target_relpath) DO UPDATE SET
                 selection_id = excluded.selection_id,
                 source_hash = excluded.source_hash,
                 source_hash_algorithm = excluded.source_hash_algorithm,
                 artifact_key = excluded.artifact_key,
                 path = excluded.path, sha256 = excluded.sha256,
                 bytes = excluded.bytes, status = excluded.status,
                 error = excluded.error, managed = excluded.managed,
                 metadata_json = excluded.metadata_json""",
            (
                export_id,
                selection_id,
                source,
                None if chart_id is None else str(chart_id),
                str(_first_value(data, "source_hash", default="") or ""),
                str(_first_value(data, "source_hash_algorithm", default="") or ""),
                str(_first_value(data, "artifact_key", default="") or ""),
                target,
                str(_first_value(data, "path", "target_path", default="") or ""),
                str(_first_value(data, "sha256", "local_sha256", default="") or ""),
                int(_first_value(data, "bytes", "bytes_count", "actual_bytes", default=0) or 0),
                str(_first_value(data, "status", "result", default=default_status) or default_status),
                str(_first_value(data, "error", "failure", default="") or ""),
                int(bool(_first_value(data, "managed", "owned", default=default_managed))),
                _json_text(_first_value(data, "metadata", default={}), {}),
            ),
        )
        row = self._conn.execute(
            """SELECT id FROM export_items
               WHERE export_id = ? AND source = ? AND chart_id IS ? AND target_relpath = ?""",
            (export_id, source, None if chart_id is None else str(chart_id), target),
        ).fetchone()
        if row is None:
            row = self._conn.execute(
                """SELECT id FROM export_items
                   WHERE export_id = ? AND source = ? AND chart_id = ? AND target_relpath = ?""",
                (export_id, source, str(chart_id), target),
            ).fetchone()
        return int(row["id"])

    def record_export_item(
        self,
        export_id: int | str,
        item: Mapping[str, Any] | Any | None = None,
        **values: Any,
    ) -> int:
        data = _as_mapping(item)
        data.update(values)
        db_id = self._export_db_id(export_id)
        with self._conn:
            return self._record_export_item_locked(db_id, data)

    save_export_item = record_export_item
    record_managed_file = record_export_item

    def get_export_record(self, export_id: int | str) -> sqlite3.Row | None:
        if isinstance(export_id, int) and not isinstance(export_id, bool):
            return self._conn.execute("SELECT * FROM export_records WHERE id = ?", (export_id,)).fetchone()
        return self._conn.execute(
            "SELECT * FROM export_records WHERE export_key = ? OR id = ?",
            (str(export_id), int(str(export_id)) if str(export_id).isdigit() else -1),
        ).fetchone()

    def list_export_records(
        self,
        workspace_id: int | str | None = None,
        *,
        status: str | None = None,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[Any] = []
        if workspace_id is not None:
            clauses.append("workspace_id = ?")
            params.append(self._workspace_db_id(workspace_id))
        if status:
            clauses.append("status = ?")
            params.append(status)
        sql = "SELECT * FROM export_records"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, id DESC"
        return list(self._conn.execute(sql, params))

    get_export_records = list_export_records

    def recover_running_exports(self) -> int:
        """Mark exports left active by a stopped process as interrupted.

        Per-item rows intentionally remain in place. They are the durable
        inspection trail used by the next build to distinguish completed work
        from charts that still need a download or validation attempt.
        """
        now = time.time()
        with self._conn:
            rows = list(
                self._conn.execute(
                    "SELECT id FROM export_records WHERE status = 'running'"
                )
            )
            if not rows:
                return 0
            ids = [int(row["id"]) for row in rows]
            placeholders = ",".join("?" for _ in ids)
            self._conn.execute(
                f"UPDATE export_records SET status = 'interrupted', completed_at = ? "
                f"WHERE id IN ({placeholders})",
                [now, *ids],
            )
            self._conn.execute(
                f"UPDATE export_items SET status = 'interrupted', "
                "error = CASE WHEN COALESCE(error, '') = '' "
                "THEN 'export interrupted; retry to resume' ELSE error END "
                f"WHERE export_id IN ({placeholders}) "
                "AND status IN ('pending', 'downloading', 'retrying', 'validated')",
                ids,
            )
        return len(ids)

    def get_export_items(self, export_id: int | str) -> list[sqlite3.Row]:
        try:
            db_id = self._export_db_id(export_id)
        except ValueError:
            return []
        return list(
            self._conn.execute(
                "SELECT * FROM export_items WHERE export_id = ? ORDER BY id",
                (db_id,),
            )
        )

    def get_managed_files(
        self,
        destination: str | None = None,
        *,
        include_failed: bool = False,
    ) -> list[sqlite3.Row]:
        clauses = ["ei.managed = 1"]
        params: list[Any] = []
        if destination:
            clauses.append("er.destination = ?")
            params.append(destination)
        if not include_failed:
            clauses.append("ei.status IN ('published', 'complete', 'success')")
        return list(
            self._conn.execute(
                """SELECT ei.*, er.export_key, er.destination, er.workspace_id
                   FROM export_items ei JOIN export_records er ON er.id = ei.export_id
                   WHERE """ + " AND ".join(clauses) + " ORDER BY ei.target_relpath",
                params,
            )
        )

    list_managed_files = get_managed_files

    def get_manifest_metadata(self, export_id: int | str) -> sqlite3.Row | None:
        try:
            db_id = self._export_db_id(export_id)
        except ValueError:
            return None
        return self._conn.execute(
            "SELECT * FROM export_manifests WHERE export_id = ?",
            (db_id,),
        ).fetchone()

    get_export_manifest = get_manifest_metadata
    record_manifest = record_export_manifest
    save_manifest = record_export_manifest

    def update_export_record(
        self,
        export_id: int | str,
        updates: Mapping[str, Any] | None = None,
        **values: Any,
    ) -> sqlite3.Row:
        db_id = self._export_db_id(export_id)
        data = dict(updates or {})
        data.update(values)
        if "metadata" in data and "metadata_json" not in data:
            data["metadata_json"] = data.pop("metadata")
        allowed = {
            "kind", "destination", "manifest_path", "status", "managed",
            "manifest_json", "metadata_json", "completed_at",
        }
        assignments: list[str] = []
        params: list[Any] = []
        for key, value in data.items():
            if key not in allowed:
                continue
            if key in {"manifest_json", "metadata_json"} and not isinstance(value, str):
                value = _json_text(value, {})
            if key == "managed":
                value = int(bool(value))
            assignments.append(f"{key} = ?")
            params.append(value)
        if "status" in data and data["status"] in {
            "complete",
            "completed",
            "success",
            "published",
            "failed",
            "error",
            "cancelled",
            "interrupted",
            "partial",
        }:
            if "completed_at" not in data:
                assignments.append("completed_at = ?")
                params.append(time.time())
        if assignments:
            params.append(db_id)
            with self._conn:
                self._conn.execute(
                    "UPDATE export_records SET " + ", ".join(assignments) + " WHERE id = ?",
                    params,
                )
        row = self.get_export_record(db_id)
        assert row is not None
        return row

    complete_export = update_export_record

    def record_export_result(
        self,
        export_id: int | str,
        result: Mapping[str, Any] | Any,
    ) -> sqlite3.Row:
        data = _as_mapping(result)
        items = data.get("items") or data.get("records") or []
        updates: dict[str, Any] = {
            "status": data.get("status")
            or ("success" if data.get("success") else "failed")
        }
        if "metadata" in data:
            updates["metadata"] = data.get("metadata")
        row = self.update_export_record(export_id, **updates)
        for item in items:
            self.record_export_item(export_id, item)
        return row
    # ---------------------------------------------------------------- tracks
    def replace_tracks(self, tracks: list[Track], file_row_counts: dict[str, int]) -> None:
        """Rebuild the playlist/track tables from a fresh CSV import."""
        with self._conn:
            # Track ids change on re-import, so per-track state is invalidated.
            self._conn.execute("DELETE FROM track_playlists")
            self._conn.execute("DELETE FROM match_scores")
            self._conn.execute("DELETE FROM selections")
            self._conn.execute("DELETE FROM spotify_tracks")
            self._conn.execute("DELETE FROM playlist_files")
            file_ids: dict[str, int] = {}
            for filename, count in file_row_counts.items():
                cur = self._conn.execute(
                    "INSERT INTO playlist_files (filename, row_count, imported_at) VALUES (?, ?, ?)",
                    (filename, count, time.time()),
                )
                file_ids[filename] = cur.lastrowid
            for track in tracks:
                cur = self._conn.execute(
                    """INSERT INTO spotify_tracks
                       (track_key, spotify_id, title, title_norm, artist_full, artist_norm,
                        primary_artist, primary_artist_norm, featured_json, album, album_norm)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        track.track_key,
                        track.spotify_id,
                        track.title,
                        track.title_norm,
                        track.artist_full,
                        track.artist_norm,
                        track.primary_artist,
                        track.primary_artist_norm,
                        json.dumps(track.featured_artists),
                        track.album,
                        track.album_norm,
                    ),
                )
                self._conn.execute(
                    """UPDATE spotify_tracks SET album_id = ?, duration_ms = ?, isrc = ?,
                       spotify_url = ?, origin = ?, origin_json = ?, origin_metadata_json = ?, added_at = ?,
                       spotify_synced_at = ? WHERE id = ?""",
                    (
                        getattr(track, "album_id", ""),
                        getattr(track, "duration_ms", None),
                        getattr(track, "isrc", ""),
                        getattr(track, "spotify_url", ""),
                        getattr(track, "origin", ""),
                        _json_text(getattr(track, "origin_metadata", {}), {}),
                        _json_text(getattr(track, "origin_metadata", {}), {}),
                        getattr(track, "added_at", None),
                        time.time(),
                        cur.lastrowid,
                    ),
                )
                track.db_id = cur.lastrowid
                for playlist in track.playlists:
                    file_id = file_ids.get(playlist)
                    if file_id is not None:
                        self._conn.execute(
                            "INSERT OR IGNORE INTO track_playlists (track_id, playlist_file_id) VALUES (?, ?)",
                            (track.db_id, file_id),
                        )

    def get_tracks(self, include_ignored: bool = False) -> list[Track]:
        sql = "SELECT * FROM spotify_tracks"
        if not include_ignored:
            sql += " WHERE ignored = 0"
        tracks = []
        for row in self._conn.execute(sql):
            track = Track(
                track_key=row["track_key"],
                title=row["title"],
                title_norm=row["title_norm"],
                artist_full=row["artist_full"],
                artist_norm=row["artist_norm"],
                primary_artist=row["primary_artist"],
                primary_artist_norm=row["primary_artist_norm"],
                featured_artists=json.loads(row["featured_json"]),
                album=row["album"] or "",
                album_norm=row["album_norm"] or "",
                spotify_id=row["spotify_id"] or "",
                db_id=row["id"],
                album_id=row["album_id"] or "" if "album_id" in row.keys() else "",
                duration_ms=row["duration_ms"] if "duration_ms" in row.keys() else None,
                isrc=row["isrc"] or "" if "isrc" in row.keys() else "",
                spotify_url=row["spotify_url"] or "" if "spotify_url" in row.keys() else "",
                origin=row["origin"] or "" if "origin" in row.keys() else "",
                origin_metadata=_load_json(
                    row["origin_json"]
                    if "origin_json" in row.keys()
                    else row["origin_metadata_json"]
                    if "origin_metadata_json" in row.keys()
                    else "{}",
                    {},
                ),
                added_at=row["added_at"] if "added_at" in row.keys() else None,
            )
            track.playlists = [
                r["filename"]
                for r in self._conn.execute(
                    """SELECT pf.filename FROM track_playlists tp
                       JOIN playlist_files pf ON pf.id = tp.playlist_file_id
                       WHERE tp.track_id = ?""",
                    (track.db_id,),
                )
            ]
            tracks.append(track)
        return tracks

    def get_master_list(self) -> dict[str, dict[str, list[Track]]]:
        """Master list: {artist: {album (or "Singles"): [tracks]}}, display-sorted.

        Computed on demand from spotify_tracks; grouping uses the primary
        artist and album (empty album falls into the Singles bucket).
        """
        from csv_to_clonehero.models import SINGLES_ALBUM_LABEL

        tree: dict[str, dict[str, list[Track]]] = {}
        for track in self.get_tracks():
            album = track.album.strip() or SINGLES_ALBUM_LABEL
            tree.setdefault(track.primary_artist, {}).setdefault(album, []).append(track)
        return {
            artist: {
                album: sorted(songs, key=lambda t: t.title.casefold())
                for album, songs in sorted(albums.items(), key=lambda kv: kv[0].casefold())
            }
            for artist, albums in sorted(tree.items(), key=lambda kv: kv[0].casefold())
        }

    def ignore_track(self, track_key: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE spotify_tracks SET ignored = 1 WHERE track_key = ?", (track_key,)
            )

    def playlist_file_counts(self) -> dict[str, int]:
        return {
            row["filename"]: row["row_count"]
            for row in self._conn.execute("SELECT filename, row_count FROM playlist_files")
        }

    # ------------------------------------------------------------- candidates
    def _archive_chart_artifact_validation(self, row: sqlite3.Row, archived_at: float) -> None:
        self._conn.execute(
            """INSERT INTO chart_artifact_validation_history
               (source,chart_id,catalog_snapshot_id,catalog_row_hash,artifact_ref,state,
                validator_version,artifact_sha256,size_bytes,format,
                playable_note_count,validation_json,reason_code,reason,
                http_status,attempt_count,first_seen_at,last_seen_at,checked_at,
                updated_at,archived_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                row["source"], row["chart_id"], row["catalog_snapshot_id"],
                row["catalog_row_hash"], row["artifact_ref"], row["state"], row["validator_version"],
                row["artifact_sha256"], row["size_bytes"], row["format"],
                row["playable_note_count"], row["validation_json"],
                row["reason_code"], row["reason"], row["http_status"],
                row["attempt_count"], row["first_seen_at"], row["last_seen_at"],
                row["checked_at"], row["updated_at"], archived_at,
            ),
        )

    def _adjust_catalog_validation_counts(
        self,
        catalog_snapshot_id: str,
        old_state: str | None,
        new_state: str | None,
    ) -> None:
        if not catalog_snapshot_id or old_state == new_state:
            return
        key = "chart_artifact_catalog_counts"
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        payload = _load_json(row["value"], {}) if row else {}
        if not isinstance(payload, dict) or payload.get("snapshot_id") != catalog_snapshot_id:
            return
        counts = payload.get("counts")
        if not isinstance(counts, dict):
            return
        if old_state:
            counts[old_state] = max(0, int(counts.get(old_state, 0)) - 1)
        if new_state:
            counts[new_state] = int(counts.get(new_state, 0)) + 1
        self._conn.execute(
            """INSERT INTO meta(key,value) VALUES(?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (key, json.dumps(payload, sort_keys=True)),
        )

    def observe_chart_artifact(
        self,
        source: str,
        chart_id: int,
        artifact_ref: str = "",
        *,
        catalog_snapshot_id: str = "",
    ) -> sqlite3.Row:
        """Remember a source identity without erasing prior validation evidence."""
        source = str(source).strip().casefold()
        chart_id = int(chart_id)
        artifact_ref = str(artifact_ref or "")
        catalog_snapshot_id = str(catalog_snapshot_id or "")
        now = time.time()
        with self._conn:
            existing = self._conn.execute(
                "SELECT * FROM chart_artifact_validations WHERE source=? AND chart_id=?",
                (source, chart_id),
            ).fetchone()
            if existing is None:
                self._conn.execute(
                    """INSERT INTO chart_artifact_validations
                       (source,chart_id,catalog_snapshot_id,artifact_ref,state,
                        first_seen_at,last_seen_at,updated_at)
                       VALUES(?,?,?,?,'UNVERIFIED',?,?,?)""",
                    (source, chart_id, catalog_snapshot_id, artifact_ref, now, now, now),
                )
                self._adjust_catalog_validation_counts(
                    catalog_snapshot_id, None, "UNVERIFIED"
                )
            elif str(existing["artifact_ref"] or "") != artifact_ref:
                self._archive_chart_artifact_validation(existing, now)
                next_snapshot_id = catalog_snapshot_id
                self._adjust_catalog_validation_counts(
                    str(existing["catalog_snapshot_id"] or ""),
                    str(existing["state"] or ""),
                    "UNVERIFIED" if next_snapshot_id == existing["catalog_snapshot_id"] else None,
                )
                if next_snapshot_id != str(existing["catalog_snapshot_id"] or ""):
                    self._adjust_catalog_validation_counts(
                        next_snapshot_id, None, "UNVERIFIED"
                    )
                self._conn.execute(
                    """UPDATE chart_artifact_validations SET catalog_snapshot_id=?,
                       catalog_row_hash='',artifact_ref=?,state='UNVERIFIED',
                       validator_version='',artifact_sha256='',size_bytes=0,format='',
                       playable_note_count=0,validation_json='{}',reason_code='',reason='',
                       http_status=NULL,checked_at=NULL,last_seen_at=?,updated_at=?
                       WHERE source=? AND chart_id=?""",
                    (next_snapshot_id, artifact_ref, now, now, source, chart_id),
                )
            else:
                old_snapshot_id = str(existing["catalog_snapshot_id"] or "")
                if catalog_snapshot_id and catalog_snapshot_id != old_snapshot_id:
                    self._archive_chart_artifact_validation(existing, now)
                    state = str(existing["state"] or "")
                    self._adjust_catalog_validation_counts(old_snapshot_id, state, None)
                    self._adjust_catalog_validation_counts(catalog_snapshot_id, None, state)
                self._conn.execute(
                    """UPDATE chart_artifact_validations
                       SET catalog_snapshot_id=CASE WHEN ?!='' THEN ? ELSE catalog_snapshot_id END,
                           catalog_row_hash=CASE
                               WHEN ?!='' AND ?!=catalog_snapshot_id THEN ''
                               ELSE catalog_row_hash END,
                           last_seen_at=?,updated_at=?
                       WHERE source=? AND chart_id=?""",
                    (
                        catalog_snapshot_id, catalog_snapshot_id,
                        catalog_snapshot_id, catalog_snapshot_id,
                        now, now, source, chart_id,
                    ),
                )
        row = self.get_chart_artifact_validation(source, chart_id)
        assert row is not None
        return row

    def record_chart_artifact_validation(
        self,
        source: str,
        chart_id: int,
        *,
        artifact_ref: str = "",
        state: str,
        validator_version: str = "",
        artifact_sha256: str = "",
        size_bytes: int = 0,
        format: str = "",
        playable_note_count: int = 0,
        validation: Mapping[str, Any] | None = None,
        reason_code: str = "",
        reason: str = "",
        http_status: int | None = None,
        catalog_snapshot_id: str = "",
    ) -> sqlite3.Row:
        """Persist one restart-safe artifact verdict while retaining identity."""
        normalized_state = str(state).strip().upper()
        if normalized_state not in {"VERIFIED", "UNVERIFIED", "REJECTED", "UNAVAILABLE"}:
            raise ValueError("invalid chart artifact validation state")
        source = str(source).strip().casefold()
        chart_id = int(chart_id)
        artifact_ref = str(artifact_ref or "")
        catalog_snapshot_id = str(catalog_snapshot_id or "")
        validator_version = str(validator_version or "")
        artifact_sha256 = str(artifact_sha256 or "")
        size_bytes = max(0, int(size_bytes or 0))
        format = str(format or "")
        playable_note_count = max(0, int(playable_note_count or 0))
        validation_json = _json_text(validation or {}, {})
        reason_code = str(reason_code or "")
        reason = str(reason or "")[:1000]
        now = time.time()
        with self._conn:
            existing = self._conn.execute(
                "SELECT * FROM chart_artifact_validations WHERE source=? AND chart_id=?",
                (source, chart_id),
            ).fetchone()
            material = (
                artifact_ref, normalized_state, validator_version, artifact_sha256,
                size_bytes, format, playable_note_count, validation_json,
                reason_code, reason, http_status,
            )
            if existing is None:
                self._conn.execute(
                    """INSERT INTO chart_artifact_validations
                       (source,chart_id,catalog_snapshot_id,artifact_ref,state,
                        validator_version,artifact_sha256,size_bytes,format,
                        playable_note_count,validation_json,reason_code,reason,
                        http_status,attempt_count,first_seen_at,last_seen_at,
                        checked_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?)""",
                    (
                        source, chart_id, catalog_snapshot_id, artifact_ref,
                        normalized_state, validator_version, artifact_sha256,
                        size_bytes, format, playable_note_count, validation_json,
                        reason_code, reason, http_status, now, now, now, now,
                    ),
                )
                self._adjust_catalog_validation_counts(
                    catalog_snapshot_id, None, normalized_state
                )
            else:
                next_snapshot_id = (
                    catalog_snapshot_id
                    if catalog_snapshot_id
                    else "" if str(existing["artifact_ref"] or "") != artifact_ref
                    else str(existing["catalog_snapshot_id"] or "")
                )
                previous = (
                    str(existing["artifact_ref"] or ""), str(existing["state"] or ""),
                    str(existing["validator_version"] or ""),
                    str(existing["artifact_sha256"] or ""), int(existing["size_bytes"] or 0),
                    str(existing["format"] or ""), int(existing["playable_note_count"] or 0),
                    str(existing["validation_json"] or "{}"),
                    str(existing["reason_code"] or ""), str(existing["reason"] or ""),
                    existing["http_status"],
                )
                old_snapshot_id = str(existing["catalog_snapshot_id"] or "")
                if previous != material or next_snapshot_id != old_snapshot_id:
                    self._archive_chart_artifact_validation(existing, now)
                if str(existing["state"] or "") != normalized_state or next_snapshot_id != old_snapshot_id:
                    self._adjust_catalog_validation_counts(
                        old_snapshot_id,
                        str(existing["state"] or ""),
                        normalized_state if next_snapshot_id == old_snapshot_id else None,
                    )
                    if next_snapshot_id != old_snapshot_id:
                        self._adjust_catalog_validation_counts(
                            next_snapshot_id, None, normalized_state
                        )
                self._conn.execute(
                    """UPDATE chart_artifact_validations SET
                       catalog_snapshot_id=?,
                       catalog_row_hash=CASE
                           WHEN artifact_ref!=? OR catalog_snapshot_id!=? THEN ''
                           ELSE catalog_row_hash END,
                       artifact_ref=?,state=?,validator_version=?,artifact_sha256=?,
                       size_bytes=?,format=?,playable_note_count=?,validation_json=?,
                       reason_code=?,reason=?,http_status=?,attempt_count=attempt_count+1,
                       last_seen_at=?,checked_at=?,updated_at=?
                       WHERE source=? AND chart_id=?""",
                    (
                        next_snapshot_id, artifact_ref, next_snapshot_id, artifact_ref,
                        normalized_state, validator_version, artifact_sha256,
                        size_bytes, format, playable_note_count, validation_json,
                        reason_code, reason, http_status, now, now, now, source, chart_id,
                    ),
                )
        row = self.get_chart_artifact_validation(source, chart_id)
        assert row is not None
        return row

    def get_chart_artifact_validation(self, source: str, chart_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM chart_artifact_validations WHERE source=? AND chart_id=?",
            (str(source).strip().casefold(), int(chart_id)),
        ).fetchone()

    def get_chart_artifact_validation_history(
        self, source: str, chart_id: int
    ) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            """SELECT * FROM chart_artifact_validation_history
               WHERE source=? AND chart_id=? ORDER BY id""",
            (str(source).strip().casefold(), int(chart_id)),
        ))

    def list_chart_artifact_validations(
        self,
        *,
        states: Iterable[str] | None = None,
        source: str = "",
        sources: Iterable[str] | None = None,
        catalog_snapshot_id: str = "",
        after_chart_id: int = 0,
        limit: int | None = None,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[Any] = []
        if int(after_chart_id) > 0:
            clauses.append("chart_id > ?")
            params.append(int(after_chart_id))
        normalized_states = sorted({str(value).upper() for value in (states or ())})
        if len(normalized_states) > 1 and limit is not None:
            rows: list[sqlite3.Row] = []
            for state in normalized_states:
                rows.extend(self.list_chart_artifact_validations(
                    states=(state,), source=source, sources=sources,
                    catalog_snapshot_id=catalog_snapshot_id,
                    after_chart_id=after_chart_id, limit=limit,
                ))
            rows.sort(key=lambda row: (
                row["checked_at"] is not None,
                float(row["checked_at"] or 0),
                str(row["source"]),
                int(row["chart_id"]),
                str(row["state"]),
            ))
            return rows[:max(1, int(limit))]
        if normalized_states:
            clauses.append("state IN (" + ",".join("?" for _ in normalized_states) + ")")
            params.extend(normalized_states)
        if source:
            clauses.append("source=?")
            params.append(str(source).strip().casefold())
        normalized_sources = sorted({
            str(value).strip().casefold() for value in (sources or ()) if str(value).strip()
        })
        if len(normalized_sources) == 1:
            clauses.append("source=?")
            params.append(normalized_sources[0])
        elif normalized_sources:
            clauses.append("source IN (" + ",".join("?" for _ in normalized_sources) + ")")
            params.extend(normalized_sources)
        if catalog_snapshot_id:
            clauses.append("catalog_snapshot_id=?")
            params.append(str(catalog_snapshot_id))
        sql = "SELECT * FROM chart_artifact_validations"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        single_source = bool(source) or len(normalized_sources) == 1
        sql += " ORDER BY checked_at,chart_id" if single_source else " ORDER BY source,checked_at,chart_id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(1, int(limit)))
        return list(self._conn.execute(sql, params))

    def get_chart_artifact_validation_counts(
        self, *, catalog_snapshot_id: str = ""
    ) -> dict[str, int]:
        counts = {state: 0 for state in ("VERIFIED", "UNVERIFIED", "REJECTED", "UNAVAILABLE")}
        if catalog_snapshot_id:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key='chart_artifact_catalog_counts'"
            ).fetchone()
            payload = _load_json(row["value"], {}) if row else {}
            if isinstance(payload, dict) and payload.get("snapshot_id") == catalog_snapshot_id:
                stored = payload.get("counts")
                if isinstance(stored, dict):
                    return {state: int(stored.get(state, 0)) for state in counts}
        sql = "SELECT state,COUNT(*) AS count FROM chart_artifact_validations"
        params: tuple[Any, ...] = ()
        if catalog_snapshot_id:
            sql += " WHERE catalog_snapshot_id=?"
            params = (str(catalog_snapshot_id),)
        sql += " GROUP BY state"
        for row in self._conn.execute(sql, params):
            counts[str(row["state"])] = int(row["count"] or 0)
        return counts

    def sync_catalog_artifacts(
        self,
        catalog_snapshot_id: str,
        candidates: Iterable[Any],
    ) -> dict[str, int | bool]:
        """Bulk-seed one catalog snapshot once; preserve changed artifacts."""
        snapshot_id = str(catalog_snapshot_id or "")
        if not snapshot_id:
            raise ValueError("catalog snapshot identity is required")
        marker_key = "chart_artifact_catalog_seed"
        if self.get_meta(marker_key) == snapshot_id:
            return {"synchronized": False, "inserted": 0, "changed": 0}
        values = [
            (
                str(getattr(candidate, "source", "") or "").strip().casefold(),
                int(getattr(candidate, "chart_id")),
                str(getattr(candidate, "md5", "") or ""),
                _catalog_candidate_row_hash(candidate),
            )
            for candidate in candidates
        ]
        now = time.time()
        with self._conn:
            self._conn.execute(
                """CREATE TEMP TABLE IF NOT EXISTS chart_artifact_catalog_seed_rows (
                   source TEXT NOT NULL, chart_id INTEGER NOT NULL,
                   artifact_ref TEXT NOT NULL, catalog_row_hash TEXT NOT NULL,
                   PRIMARY KEY(source,chart_id))"""
            )
            self._conn.execute("DELETE FROM chart_artifact_catalog_seed_rows")
            self._conn.executemany(
                """INSERT OR REPLACE INTO chart_artifact_catalog_seed_rows
                   (source,chart_id,artifact_ref,catalog_row_hash) VALUES(?,?,?,?)""",
                values,
            )
            changed = int(self._conn.execute(
                """SELECT COUNT(*) AS count FROM chart_artifact_validations current
                   JOIN chart_artifact_catalog_seed_rows seed
                     ON seed.source=current.source AND seed.chart_id=current.chart_id
                   WHERE current.catalog_snapshot_id!=?
                      OR seed.artifact_ref!=current.artifact_ref
                      OR seed.catalog_row_hash!=current.catalog_row_hash""",
                (snapshot_id,),
            ).fetchone()["count"] or 0)
            self._conn.execute(
                """INSERT INTO chart_artifact_validation_history
                   (source,chart_id,catalog_snapshot_id,catalog_row_hash,artifact_ref,state,
                    validator_version,artifact_sha256,size_bytes,format,
                    playable_note_count,validation_json,reason_code,reason,
                    http_status,attempt_count,first_seen_at,last_seen_at,checked_at,
                    updated_at,archived_at)
                   SELECT current.source,current.chart_id,current.catalog_snapshot_id,
                          current.catalog_row_hash,current.artifact_ref,current.state,current.validator_version,
                          current.artifact_sha256,current.size_bytes,current.format,
                          current.playable_note_count,current.validation_json,
                          current.reason_code,current.reason,current.http_status,
                          current.attempt_count,current.first_seen_at,current.last_seen_at,
                          current.checked_at,current.updated_at,?
                     FROM chart_artifact_validations current
                     JOIN chart_artifact_catalog_seed_rows seed
                       ON seed.source=current.source AND seed.chart_id=current.chart_id
                    WHERE current.catalog_snapshot_id!=?
                       OR seed.artifact_ref!=current.artifact_ref
                       OR seed.catalog_row_hash!=current.catalog_row_hash""",
                (now, snapshot_id),
            )
            self._conn.execute(
                """UPDATE chart_artifact_validations SET
                   artifact_ref=(SELECT seed.artifact_ref FROM chart_artifact_catalog_seed_rows seed
                                 WHERE seed.source=chart_artifact_validations.source
                                   AND seed.chart_id=chart_artifact_validations.chart_id),
                   catalog_snapshot_id=?,
                   catalog_row_hash=(SELECT seed.catalog_row_hash FROM chart_artifact_catalog_seed_rows seed
                                     WHERE seed.source=chart_artifact_validations.source
                                       AND seed.chart_id=chart_artifact_validations.chart_id),
                   state='UNVERIFIED',validator_version='',
                   artifact_sha256='',size_bytes=0,format='',playable_note_count=0,
                   validation_json='{}',reason_code='',reason='',http_status=NULL,
                   checked_at=NULL,last_seen_at=?,updated_at=?
                   WHERE EXISTS (SELECT 1 FROM chart_artifact_catalog_seed_rows seed
                                 WHERE seed.source=chart_artifact_validations.source
                                   AND seed.chart_id=chart_artifact_validations.chart_id
                                   AND (seed.artifact_ref!=chart_artifact_validations.artifact_ref
                                        OR seed.catalog_row_hash!=chart_artifact_validations.catalog_row_hash))""",
                (snapshot_id, now, now),
            )
            self._conn.execute(
                """UPDATE chart_artifact_validations SET catalog_snapshot_id=?,
                   catalog_row_hash=(SELECT seed.catalog_row_hash FROM chart_artifact_catalog_seed_rows seed
                                     WHERE seed.source=chart_artifact_validations.source
                                       AND seed.chart_id=chart_artifact_validations.chart_id),
                   last_seen_at=?,updated_at=?
                   WHERE EXISTS (SELECT 1 FROM chart_artifact_catalog_seed_rows seed
                                 WHERE seed.source=chart_artifact_validations.source
                                   AND seed.chart_id=chart_artifact_validations.chart_id)""",
                (snapshot_id, now, now),
            )
            inserted = int(self._conn.execute(
                """SELECT COUNT(*) AS count FROM chart_artifact_catalog_seed_rows seed
                   WHERE NOT EXISTS (SELECT 1 FROM chart_artifact_validations current
                                     WHERE current.source=seed.source
                                       AND current.chart_id=seed.chart_id)"""
            ).fetchone()["count"] or 0)
            self._conn.execute(
                """INSERT INTO chart_artifact_validations
                   (source,chart_id,catalog_snapshot_id,catalog_row_hash,artifact_ref,state,
                    first_seen_at,last_seen_at,updated_at)
                   SELECT seed.source,seed.chart_id,?,seed.catalog_row_hash,
                          seed.artifact_ref,'UNVERIFIED',?,?,?
                     FROM chart_artifact_catalog_seed_rows seed
                    WHERE NOT EXISTS (SELECT 1 FROM chart_artifact_validations current
                                      WHERE current.source=seed.source
                                        AND current.chart_id=seed.chart_id)""",
                (snapshot_id, now, now, now),
            )
            self._conn.execute(
                """INSERT INTO meta(key,value) VALUES(?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (marker_key, snapshot_id),
            )
            counts = {state: 0 for state in ("VERIFIED", "UNVERIFIED", "REJECTED", "UNAVAILABLE")}
            for row in self._conn.execute(
                """SELECT state,COUNT(*) AS count FROM chart_artifact_validations
                   WHERE catalog_snapshot_id=? GROUP BY state""",
                (snapshot_id,),
            ):
                counts[str(row["state"])] = int(row["count"] or 0)
            self._conn.execute(
                """INSERT INTO meta(key,value) VALUES('chart_artifact_catalog_counts',?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (json.dumps({"snapshot_id": snapshot_id, "counts": counts}, sort_keys=True),),
            )
            self._conn.execute("DELETE FROM chart_artifact_catalog_seed_rows")
        return {"synchronized": True, "inserted": inserted, "changed": changed}

    def get_blocked_chart_identities(self) -> set[tuple[str, int]]:
        return {
            (str(row["source"]), int(row["chart_id"]))
            for row in self._conn.execute(
                "SELECT source,chart_id FROM chart_artifact_validations WHERE state IN ('REJECTED','UNAVAILABLE')"
            )
        }

    def chart_validation_fingerprint(self) -> str:
        """Fingerprint only ledger rows that alter catalog eligibility."""
        digest = hashlib.sha256()
        for row in self._conn.execute(
            """SELECT source,chart_id,artifact_ref,state,validator_version
               FROM chart_artifact_validations
               WHERE state IN ('REJECTED','UNAVAILABLE')
               ORDER BY source,chart_id"""
        ):
            digest.update(json.dumps(tuple(row), separators=(",", ":")).encode("utf-8"))
            digest.update(b"\n")
        return digest.hexdigest()

    def is_chart_artifact_eligible(self, source: str, chart_id: int) -> bool:
        row = self.get_chart_artifact_validation(source, chart_id)
        return row is None or str(row["state"]) not in {"REJECTED", "UNAVAILABLE"}

    def upsert_candidate(self, candidate: ChartCandidate | Mapping[str, Any] | Any) -> ChartCandidate:
        candidate = _coerce_candidate(candidate)
        data = _as_mapping(candidate)
        with self._conn:
            self._conn.execute(
                """INSERT INTO candidate_charts
                   (source, chart_id, md5, data_json, source_artwork_url,
                    source_artwork_id, issue_details_json, issue_severity,
                    review_status, review_url, song_id, version_id,
                    version_group_id, group_id, source_total, cap_reached,
                    returned_count, source_metadata_json, diff_band,
                    diff_guitar_coop, diff_rhythm, diff_drums_real,
                    diff_guitar_coop_ghl, diff_rhythm_ghl, diff_bassghl,
                    diff_guitar_ghl, diff_bass_ghl, diff_vocals_harm1,
                    diff_vocals_harm2, diff_vocals_harm3, difficulty_fields_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source, chart_id) DO UPDATE SET
                       md5 = excluded.md5, data_json = excluded.data_json,
                       source_artwork_url = excluded.source_artwork_url,
                       source_artwork_id = excluded.source_artwork_id,
                       issue_details_json = excluded.issue_details_json,
                       issue_severity = excluded.issue_severity,
                       review_status = excluded.review_status,
                       review_url = excluded.review_url,
                       song_id = excluded.song_id, version_id = excluded.version_id,
                       version_group_id = excluded.version_group_id,
                       group_id = excluded.group_id,
                       source_total = excluded.source_total,
                       cap_reached = excluded.cap_reached,
                       returned_count = excluded.returned_count,
                       source_metadata_json = excluded.source_metadata_json,
                       diff_band = excluded.diff_band,
                       diff_guitar_coop = excluded.diff_guitar_coop,
                       diff_rhythm = excluded.diff_rhythm,
                       diff_drums_real = excluded.diff_drums_real,
                       diff_guitar_coop_ghl = excluded.diff_guitar_coop_ghl,
                       diff_rhythm_ghl = excluded.diff_rhythm_ghl,
                       diff_bassghl = excluded.diff_bassghl,
                       diff_guitar_ghl = excluded.diff_guitar_ghl,
                       diff_bass_ghl = excluded.diff_bass_ghl,
                       diff_vocals_harm1 = excluded.diff_vocals_harm1,
                       diff_vocals_harm2 = excluded.diff_vocals_harm2,
                       diff_vocals_harm3 = excluded.diff_vocals_harm3,
                       difficulty_fields_json = excluded.difficulty_fields_json""",
                (
                    candidate.source,
                    candidate.chart_id,
                    candidate.md5,
                    _candidate_to_json(candidate),
                    _first_value(data, "source_artwork_url", "artwork_url", default=""),
                    _first_value(data, "source_artwork_id", "artwork_id", default=""),
                    _json_text(
                        _first_value(data, "issue_details", "issues", default=[]), []
                    ),
                    candidate.issue_severity,
                    candidate.review_status,
                    candidate.review_url,
                    candidate.song_id,
                    candidate.version_id,
                    candidate.version_group_id,
                    candidate.group_id,
                    candidate.source_total,
                    int(candidate.cap_reached),
                    candidate.returned_count,
                    _json_text(candidate.source_metadata, {}),
                    candidate.diff_band,
                    candidate.diff_guitar_coop,
                    candidate.diff_rhythm,
                    candidate.diff_drums_real,
                    candidate.diff_guitar_coop_ghl,
                    candidate.diff_rhythm_ghl,
                    candidate.diff_bassghl,
                    candidate.diff_guitar_ghl,
                    candidate.diff_bass_ghl,
                    candidate.diff_vocals_harm1,
                    candidate.diff_vocals_harm2,
                    candidate.diff_vocals_harm3,
                    _json_text(
                        _first_value(
                            data,
                            "difficulty_fields",
                            "difficulties",
                            default={},
                        ),
                        {},
                    ),
                ),
            )
            row = self._conn.execute(
                "SELECT id FROM candidate_charts WHERE source = ? AND chart_id = ?",
                (candidate.source, candidate.chart_id),
            ).fetchone()
        candidate.db_id = row["id"]
        self.observe_chart_artifact(candidate.source, candidate.chart_id, candidate.md5 or "")
        return candidate

    def get_candidate(self, db_id: int) -> ChartCandidate | None:
        row = self._conn.execute(
            "SELECT id, data_json FROM candidate_charts WHERE id = ?", (db_id,)
        ).fetchone()
        return _candidate_from_json(row["id"], row["data_json"]) if row else None

    # -------------------------------------------------------- search caching
    @staticmethod
    def query_key(
        kind: str,
        *parts: str,
        source_name: str = "",
        source_version: str = "",
        result_cap: int | None = None,
        matcher_version: str = "",
        filters: Mapping[str, Any] | None = None,
    ) -> str:
        """Build a stable cache key while preserving the legacy form.

        Optional discovery dimensions are included only when supplied, so old
        callers produce the exact same key they did before workspace support.
        """
        raw_value: list[Any] = [kind, *parts]
        if any((source_name, source_version, result_cap is not None, matcher_version, filters)):
            raw_value.extend(
                [
                    {"source": source_name, "version": source_version},
                    result_cap,
                    matcher_version,
                    filters or {},
                ]
            )
        raw = json.dumps(raw_value, sort_keys=True, default=str)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _fresh_cached_row(
        self,
        query_key: str,
        ttl_days: int,
        *,
        requested_cap: int | None,
        source_name: str = "",
        source_version: str = "",
    ) -> sqlite3.Row | None:
        """Return the ``search_queries`` row when it is fresh and compatible.

        This is the cheap part of a cache hit: one indexed lookup and a few
        comparisons. It never materializes cached candidates, so callers that
        only need freshness (estimates, planners) stay fast on large caches.
        """
        row = self._conn.execute(
            """SELECT id, searched_at, error, result_cap, source_total,
                      cap_reached, returned_count, complete, source_name,
                      source_version
               FROM search_queries WHERE query_key = ?""",
            (query_key,),
        ).fetchone()
        if row is None:
            return None
        if ttl_days >= 0 and time.time() - row["searched_at"] > ttl_days * 86400:
            return None
        if source_name and row["source_name"] and row["source_name"] != source_name:
            return None
        if source_version and row["source_version"] and row["source_version"] != source_version:
            return None
        if requested_cap is not None:
            cached_cap = row["result_cap"]
            returned_count = row["returned_count"] or 0
            if cached_cap is not None and cached_cap < requested_cap:
                if row["cap_reached"] or not row["complete"]:
                    return None
            elif cached_cap is None and row["cap_reached"] and returned_count < requested_cap:
                return None
        return row

    def fresh_cached_query(
        self,
        query_key: str,
        ttl_days: int,
        *,
        result_cap: int | None = None,
        cap: int | None = None,
        max_results: int | None = None,
        source_name: str = "",
        source_version: str = "",
    ) -> sqlite3.Row | None:
        """Return cached-query metadata when the entry is a usable hit."""
        requested_cap = result_cap if result_cap is not None else cap
        if requested_cap is None:
            requested_cap = max_results
        return self._fresh_cached_row(
            query_key,
            ttl_days,
            requested_cap=requested_cap,
            source_name=source_name,
            source_version=source_version,
        )

    def cached_query_is_fresh(
        self,
        query_key: str,
        ttl_days: int,
        *,
        result_cap: int | None = None,
        cap: int | None = None,
        max_results: int | None = None,
        source_name: str = "",
        source_version: str = "",
    ) -> bool:
        """Cheap freshness check that never materializes cached candidates."""
        return (
            self.fresh_cached_query(
                query_key,
                ttl_days,
                result_cap=result_cap,
                cap=cap,
                max_results=max_results,
                source_name=source_name,
                source_version=source_version,
            )
            is not None
        )

    def get_cached_query(
        self,
        query_key: str,
        ttl_days: int,
        *,
        result_cap: int | None = None,
        cap: int | None = None,
        max_results: int | None = None,
        source_name: str = "",
        source_version: str = "",
        workspace_id: int | str | None = None,
    ) -> tuple[list[ChartCandidate], str | None] | None:
        """Return ``(candidates, error)`` for a fresh, cap-compatible query.

        The original two-argument API remains valid.  New callers can request
        a larger cap; a cached capped/partial response is then treated as a
        miss instead of silently masquerading as a complete catalog.
        """
        requested_cap = result_cap if result_cap is not None else cap
        if requested_cap is None:
            requested_cap = max_results
        row = self._fresh_cached_row(
            query_key,
            ttl_days,
            requested_cap=requested_cap,
            source_name=source_name,
            source_version=source_version,
        )
        if row is None:
            return None
        candidates = [
            self.get_candidate(cid)
            for cid in self._query_candidate_ids(row["id"])
        ]
        clean_candidates = [c for c in candidates if c is not None]
        if requested_cap is not None:
            clean_candidates = clean_candidates[:requested_cap]
        return clean_candidates, row["error"]

    def get_cached_query_metadata(self, query_key: str) -> sqlite3.Row | None:
        """Return cap/progress metadata for a cached query, if present."""
        return self._conn.execute(
            "SELECT * FROM search_queries WHERE query_key = ?", (query_key,)
        ).fetchone()

    get_query_metadata = get_cached_query_metadata
    get_query = get_cached_query_metadata
    get_cached_query_meta = get_cached_query_metadata
    get_query_cache = get_cached_query
    get_cached_search = get_cached_query

    def record_query(
        self,
        query_key: str,
        kind: str,
        query_text: str,
        candidates: list[ChartCandidate],
        error: str | None = None,
        *,
        source_name: str = "",
        source_version: str = "",
        result_cap: int | None = None,
        cap: int | None = None,
        source_total: int | None = None,
        total: int | None = None,
        cap_reached: bool = False,
        returned_count: int | None = None,
        complete: bool = True,
        metadata: Mapping[str, Any] | None = None,
        metadata_json: str | None = None,
        workspace_id: int | str | None = None,
        task_id: int | None = None,
    ) -> None:
        candidates = list(candidates)
        if source_total is None:
            source_total = total
        actual_cap = result_cap if result_cap is not None else cap
        actual_returned = len(candidates) if returned_count is None else returned_count
        actual_metadata = metadata_json or _json_text(metadata or {}, {})
        with self._conn:
            self._conn.execute(
                """INSERT INTO search_queries
                   (query_key, kind, query_text, searched_at, result_count, error,
                    source_name, source_version, result_cap, source_total,
                    cap_reached, returned_count, complete, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(query_key) DO UPDATE SET
                       searched_at = excluded.searched_at,
                       result_count = excluded.result_count,
                       error = excluded.error,
                       source_name = excluded.source_name,
                       source_version = excluded.source_version,
                       result_cap = excluded.result_cap,
                       source_total = excluded.source_total,
                       cap_reached = excluded.cap_reached,
                       returned_count = excluded.returned_count,
                       complete = excluded.complete,
                       metadata_json = excluded.metadata_json""",
                (
                    query_key,
                    kind,
                    query_text,
                    time.time(),
                    len(candidates),
                    error,
                    source_name,
                    source_version,
                    actual_cap,
                    source_total,
                    int(cap_reached),
                    actual_returned,
                    int(complete),
                    actual_metadata,
                ),
            )
            row = self._conn.execute(
                "SELECT id FROM search_queries WHERE query_key = ?", (query_key,)
            ).fetchone()
            query_id = row["id"]
            self._conn.execute(
                "DELETE FROM search_query_candidates WHERE query_id = ?", (query_id,)
            )
            for candidate in candidates:
                candidate = _coerce_candidate(candidate)
                if candidate.db_id is None or self.get_candidate(candidate.db_id) is None:
                    candidate = self.upsert_candidate(candidate)
                self._conn.execute(
                    "INSERT OR IGNORE INTO search_query_candidates (query_id, candidate_id) VALUES (?, ?)",
                    (query_id, candidate.db_id),
                )
            if task_id is not None:
                self._conn.execute(
                    """UPDATE discovery_tasks SET status = ?, source_total = ?,
                       result_cap = ?, returned_count = ?, cap_reached = ?,
                       error = ?, finished_at = ? WHERE id = ?""",
                    (
                        "error" if error else "completed" if complete else "partial",
                        source_total,
                        actual_cap,
                        actual_returned,
                        int(cap_reached),
                        error,
                        time.time() if complete or error else None,
                        task_id,
                    ),
                )

    record_cached_query = record_query
    cache_query = record_query

    def _query_candidate_ids(self, query_id: int) -> list[int]:
        return [
            r["candidate_id"]
            for r in self._conn.execute(
                "SELECT candidate_id FROM search_query_candidates WHERE query_id = ?",
                (query_id,),
            )
        ]

    def reset_search_cache(self) -> int:
        with self._conn:
            count = self._conn.execute("SELECT COUNT(*) AS c FROM search_queries").fetchone()["c"]
            self._conn.execute("DELETE FROM search_query_candidates")
            self._conn.execute("DELETE FROM search_queries")
            self._conn.execute("DELETE FROM match_scores")
            self._conn.execute("DELETE FROM expansion_candidates")
            self._conn.execute("DELETE FROM selections")
        return count

    # ------------------------------------------------------------ match scores
    def clear_match_scores(self) -> None:
        """Remove scores before rebuilding the exact pass."""
        with self._conn:
            self._conn.execute("DELETE FROM match_scores")

    def record_match_score(
        self, track_db_id: int, candidate_db_id: int, score: float, decision: str, breakdown_json: str
    ) -> None:
        with self._conn:
            self._conn.execute(
                """INSERT INTO match_scores (track_id, candidate_id, score, decision, breakdown_json)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(track_id, candidate_id) DO UPDATE SET
                       score = excluded.score, decision = excluded.decision,
                       breakdown_json = excluded.breakdown_json""",
                (track_db_id, candidate_db_id, score, decision, breakdown_json),
            )

    def get_match_scores(self) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                """SELECT ms.*, t.title AS track_title, t.artist_full AS track_artist,
                           t.track_key, c.data_json AS candidate_json, c.id AS candidate_db_id
                   FROM match_scores ms
                   JOIN spotify_tracks t ON t.id = ms.track_id
                   JOIN candidate_charts c ON c.id = ms.candidate_id"""
            )
        )

    def get_match_scores_for_track(self, track_db_id: int) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                """SELECT ms.*, t.title AS track_title, t.artist_full AS track_artist,
                          t.track_key, c.data_json AS candidate_json, c.id AS candidate_db_id
                   FROM match_scores ms
                   JOIN spotify_tracks t ON t.id = ms.track_id
                   JOIN candidate_charts c ON c.id = ms.candidate_id
                   WHERE ms.track_id = ?""",
                (track_db_id,),
            )
        )

    def delete_match_scores_for_track(self, track_db_id: int) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM match_scores WHERE track_id = ?", (track_db_id,))

    # ------------------------------------------------------------ expansion
    def replace_expansion_artists(self, artists: list[dict]) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM expansion_candidates")
            self._conn.execute("DELETE FROM expansion_artists")
            for a in artists:
                self._conn.execute(
                    """INSERT INTO expansion_artists
                       (artist_name, artist_norm, track_count, is_featured_only, cap, disabled)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        a["artist_name"],
                        a["artist_norm"],
                        a["track_count"],
                        int(a["is_featured_only"]),
                        a["cap"],
                        int(a["disabled"]),
                    ),
                )

    def get_expansion_artists(self) -> list[sqlite3.Row]:
        return list(self._conn.execute("SELECT * FROM expansion_artists"))

    def record_expansion_candidate(
        self, artist_db_id: int, candidate_db_id: int, score: float, breakdown_json: str
    ) -> None:
        with self._conn:
            self._conn.execute(
                """INSERT INTO expansion_candidates (artist_id, candidate_id, score, breakdown_json)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(artist_id, candidate_id) DO UPDATE SET
                       score = excluded.score, breakdown_json = excluded.breakdown_json""",
                (artist_db_id, candidate_db_id, score, breakdown_json),
            )

    def get_expansion_candidates(self) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                """SELECT ec.*, ea.artist_name, ea.artist_norm, c.data_json AS candidate_json
                   FROM expansion_candidates ec
                   JOIN expansion_artists ea ON ea.id = ec.artist_id
                   JOIN candidate_charts c ON c.id = ec.candidate_id"""
            )
        )

    # ------------------------------------------------------------ selections
    def replace_selections(self, selections: list[dict]) -> None:
        with self._conn:
            # downloads are intentionally kept: they key on candidate_id and
            # enable resume/idempotent rebuilds.
            self._conn.execute("DELETE FROM selections")
            for s in selections:
                self._conn.execute(
                    """INSERT INTO selections
                       (kind, track_id, artist_norm, candidate_id, priority_tier, status,
                        score, explanation, planned_bytes, target_relpath)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        s["kind"],
                        s.get("track_id"),
                        s.get("artist_norm"),
                        s["candidate_id"],
                        s["priority_tier"],
                        s["status"],
                        s["score"],
                        s.get("explanation", ""),
                        s.get("planned_bytes", 0),
                        s.get("target_relpath", ""),
                    ),
                )

    def get_selections(self, status: str | None = None) -> list[sqlite3.Row]:
        sql = """SELECT s.*, c.data_json AS candidate_json
                 FROM selections s JOIN candidate_charts c ON c.id = s.candidate_id"""
        if status:
            sql += " WHERE s.status = ?"
            return list(self._conn.execute(sql, (status,)))
        return list(self._conn.execute(sql))

    # -------------------------------------------------------------- downloads
    def record_download(
        self,
        candidate_id: int,
        status: str,
        bytes_count: int = 0,
        path: str = "",
        md5: str = "",
        error: str = "",
        sha256: str = "",
        validation_json: str | Mapping[str, Any] = "",
    ) -> None:
        validation_text = (
            _json_text(validation_json, {})
            if isinstance(validation_json, Mapping)
            else str(validation_json or "{}")
        )
        with self._conn:
            self._conn.execute(
                """INSERT INTO downloads
                   (candidate_id, status, bytes, path, md5, sha256, validation_json, downloaded_at, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(candidate_id) DO UPDATE SET
                       status = excluded.status, bytes = excluded.bytes, path = excluded.path,
                       md5 = excluded.md5, sha256 = excluded.sha256,
                       validation_json = excluded.validation_json,
                       downloaded_at = excluded.downloaded_at,
                       error = excluded.error""",
                (
                    candidate_id,
                    status,
                    bytes_count,
                    path,
                    md5,
                    sha256,
                    validation_text,
                    time.time(),
                    error,
                ),
            )

    def update_download_path(self, candidate_id: int, path: str) -> None:
        """Repoint a completed download at its relocated file."""
        with self._conn:
            self._conn.execute(
                "UPDATE downloads SET path = ? WHERE candidate_id = ?",
                (path, candidate_id),
            )

    def get_download_for_candidate(self, candidate_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM downloads WHERE candidate_id = ?", (candidate_id,)
        ).fetchone()

    def get_downloads(self, status: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM downloads"
        if status:
            sql += " WHERE status = ?"
            return list(self._conn.execute(sql, (status,)))
        return list(self._conn.execute(sql))

    # ---------------------------------------------------- manual decisions
    def set_manual_decision(
        self, track_key: str, source: str, chart_id: int, decision: str
    ) -> None:
        """Record a human accept/reject; decision='clear' removes it."""
        if decision not in ("accept", "reject", "clear"):
            raise ValueError("decision must be accept, reject, or clear")
        with self._conn:
            if decision == "clear":
                self._conn.execute(
                    "DELETE FROM manual_decisions WHERE track_key = ? AND source = ? AND chart_id = ?",
                    (track_key, source, chart_id),
                )
            else:
                # An accepted chart is definitive for the track. Rejections
                # remain intact so clearing the accept can fall back safely.
                if decision == "accept":
                    self._conn.execute(
                        "DELETE FROM manual_decisions "
                        "WHERE track_key = ? AND decision = 'accept' "
                        "AND NOT (source = ? AND chart_id = ?)",
                        (track_key, source, chart_id),
                    )
                self._conn.execute(
                    """INSERT INTO manual_decisions (track_key, source, chart_id, decision, created_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(track_key, source, chart_id) DO UPDATE SET
                           decision = excluded.decision, created_at = excluded.created_at""",
                    (track_key, source, chart_id, decision, time.time()),
                )

    def get_manual_decisions(self) -> dict[tuple[str, str, int], str]:
        """Return {(track_key, source, chart_id): 'accept' | 'reject'}."""
        return {
            (row["track_key"], row["source"], row["chart_id"]): row["decision"]
            for row in self._conn.execute(
                "SELECT track_key, source, chart_id, decision FROM manual_decisions"
            )
        }

    def accept_best_review_candidates(self) -> dict[str, int]:
        """Accept the highest-scoring current review candidate per track.

        Existing accepts are left unchanged and explicit rejects are skipped.
        All decisions are persisted in one SQLite transaction so the web bulk
        action cannot leave a track with multiple accepted winners.
        """
        decisions = self.get_manual_decisions()
        existing_accepts = {
            track_key
            for (track_key, _source, _chart_id), decision in decisions.items()
            if decision == "accept"
        }
        auto_track_ids = {
            row["track_id"]
            for row in self._conn.execute(
                "SELECT track_id FROM match_scores WHERE decision = 'AUTO_ACCEPT'"
            )
        }
        rows = self._conn.execute(
            """SELECT ms.track_id, ms.score, t.track_key, c.source, c.chart_id
               FROM match_scores ms
               JOIN spotify_tracks t ON t.id = ms.track_id
               JOIN candidate_charts c ON c.id = ms.candidate_id
               WHERE ms.decision = 'REVIEW'
               ORDER BY ms.track_id, ms.score DESC, c.source, c.chart_id"""
        )
        best: dict[str, tuple[str, int]] = {}
        for row in rows:
            track_key = row["track_key"]
            if (
                row["track_id"] in auto_track_ids
                or track_key in existing_accepts
                or track_key in best
            ):
                continue
            if decisions.get((track_key, row["source"], row["chart_id"])) == "reject":
                continue
            best[track_key] = (row["source"], row["chart_id"])

        accepted = 0
        with self._conn:
            for track_key, (source, chart_id) in best.items():
                self._conn.execute(
                    "DELETE FROM manual_decisions "
                    "WHERE track_key = ? AND decision = 'accept' "
                    "AND NOT (source = ? AND chart_id = ?)",
                    (track_key, source, chart_id),
                )
                self._conn.execute(
                    """INSERT INTO manual_decisions
                       (track_key, source, chart_id, decision, created_at)
                       VALUES (?, ?, ?, 'accept', ?)
                       ON CONFLICT(track_key, source, chart_id) DO UPDATE SET
                           decision = excluded.decision, created_at = excluded.created_at""",
                    (track_key, source, chart_id, time.time()),
                )
                accepted += 1
        return {"available": len(best), "accepted": accepted}

    # ----------------------------------------------------------------- errors
    def record_error(self, stage: str, message: str, detail: str = "") -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO errors (stage, message, detail, occurred_at) VALUES (?, ?, ?, ?)",
                (stage, message, detail, time.time()),
            )

    def get_errors(self) -> list[sqlite3.Row]:
        return list(self._conn.execute("SELECT * FROM errors ORDER BY occurred_at"))

    # -------------------------------------------------- multi-user (cloud)
    # Per-user identity is the Spotify login. The discovery engine scopes a
    # workspace by an opaque, globally-unique key, so each user's workspaces
    # are isolated by suffixing the key with their user id. The 7-day query
    # cache and candidate_charts stay global on purpose (shared across users
    # keeps a public app under Enchor's per-IP rate limit).

    @staticmethod
    def workspace_key_for_user(spotify_user_id: str, base: str = "spotify") -> str:
        """Return a globally-unique, per-user workspace key (sanitized)."""
        safe = str(spotify_user_id or "anon").strip().casefold()
        safe = "".join(ch for ch in safe if ch.isalnum() or ch in "._-")
        if not safe:
            safe = "anon"
        return f"{base}:u:{safe[:64]}"

    # ---- users ------------------------------------------------------------
    def upsert_user(self, *, spotify_user_id: str, display_name: str = "", **fields: Any) -> sqlite3.Row:
        spotify_user_id = str(spotify_user_id or "").strip()
        if not spotify_user_id:
            raise ValueError("spotify_user_id is required")
        now = time.time()
        with self._conn:
            self._conn.execute(
                """INSERT INTO users (spotify_user_id, display_name, country, product, href, scopes_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(spotify_user_id) DO UPDATE SET
                     display_name = COALESCE(NULLIF(excluded.display_name, ''), users.display_name),
                     country = COALESCE(NULLIF(excluded.country, ''), users.country),
                     product = COALESCE(NULLIF(excluded.product, ''), users.product),
                     href = COALESCE(NULLIF(excluded.href, ''), users.href),
                     scopes_json = excluded.scopes_json,
                     updated_at = excluded.updated_at""",
                (
                    spotify_user_id,
                    str(display_name or ""),
                    str(fields.get("country") or ""),
                    str(fields.get("product") or ""),
                    str(fields.get("href") or ""),
                    _json_text(fields.get("scopes") or [], []),
                    now,
                    now,
                ),
            )
        return self.get_user(spotify_user_id)

    def get_user(self, user_id: int | str) -> sqlite3.Row | None:
        if isinstance(user_id, int) and not isinstance(user_id, bool):
            return self._conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return self._conn.execute("SELECT * FROM users WHERE spotify_user_id = ?", (str(user_id),)).fetchone()

    get_user_by_spotify_id = get_user

    # ---- sessions ---------------------------------------------------------
    def create_session(self, user_id: int, *, expires_at: float, user_agent: str = "") -> sqlite3.Row:
        now = time.time()
        session_id = uuid.uuid4().hex
        with self._conn:
            self._conn.execute(
                "INSERT INTO user_sessions (session_id, user_id, created_at, last_seen_at, expires_at, revoked, user_agent) "
                "VALUES (?, ?, ?, ?, ?, 0, ?)",
                (session_id, user_id, now, now, float(expires_at), str(user_agent or "")[:500]),
            )
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM user_sessions WHERE session_id = ?", (str(session_id),)).fetchone()

    def touch_session(self, session_id: str, *, now: float | None = None) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE user_sessions SET last_seen_at = ? WHERE session_id = ?",
                (time.time() if now is None else now, str(session_id)),
            )

    def revoke_session(self, session_id: str) -> None:
        with self._conn:
            self._conn.execute("UPDATE user_sessions SET revoked = 1 WHERE session_id = ?", (str(session_id),))

    def delete_expired_sessions(self, *, now: float | None = None) -> int:
        now = time.time() if now is None else now
        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM user_sessions WHERE expires_at < ? OR revoked = 1", (now,)
            )
            return int(cursor.rowcount or 0)

    def get_user_by_session(self, session_id: str) -> sqlite3.Row | None:
        row = self._conn.execute(
            """SELECT u.* FROM user_sessions s
               JOIN users u ON u.id = s.user_id
               WHERE s.session_id = ? AND s.revoked = 0 AND s.expires_at > ?""",
            (str(session_id), time.time()),
        ).fetchone()
        if row is not None:
            with self._conn:
                self._conn.execute(
                    "UPDATE user_sessions SET last_seen_at = ? WHERE session_id = ?",
                    (time.time(), str(session_id)),
                )
        return row

    # ---- tokens (encrypted at rest; server never sees plaintext) ----------
    def set_user_token(self, user_id: int, encrypted_refresh_token: str, *, scope: Iterable[str] = ()) -> None:
        with self._conn:
            self._conn.execute(
                """INSERT INTO user_tokens (user_id, encrypted_refresh_token, scope_json, rotate_version, updated_at)
                   VALUES (?, ?, ?, 1, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     encrypted_refresh_token = excluded.encrypted_refresh_token,
                     scope_json = excluded.scope_json,
                     rotate_version = user_tokens.rotate_version + 1,
                     updated_at = excluded.updated_at""",
                (user_id, str(encrypted_refresh_token or ""), _json_text(list(scope), []), time.time()),
            )

    def get_user_token(self, user_id: int) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM user_tokens WHERE user_id = ?", (user_id,)).fetchone()

    def clear_user_token(self, user_id: int) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM user_tokens WHERE user_id = ?", (user_id,))

    def set_provider_token(
        self, user_id: int, provider: str, encrypted_refresh_token: str,
        scope: Iterable[str] = (),
    ) -> None:
        """Store a provider-scoped encrypted refresh token."""
        with self._conn:
            self._conn.execute(
                """INSERT INTO provider_tokens
                   (user_id, provider, encrypted_refresh_token, scope_json, rotate_version, updated_at)
                   VALUES (?, ?, ?, ?, 1, ?)
                   ON CONFLICT(user_id, provider) DO UPDATE SET
                     encrypted_refresh_token = excluded.encrypted_refresh_token,
                     scope_json = excluded.scope_json,
                     rotate_version = provider_tokens.rotate_version + 1,
                     updated_at = excluded.updated_at""",
                (int(user_id), str(provider), str(encrypted_refresh_token or ""), _json_text(list(scope), []), time.time()),
            )

    def get_provider_token(self, user_id: int, provider: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM provider_tokens WHERE user_id = ? AND provider = ?",
            (int(user_id), str(provider)),
        ).fetchone()

    def clear_provider_token(self, user_id: int, provider: str) -> None:
        with self._conn:
            self._conn.execute(
                "DELETE FROM provider_tokens WHERE user_id = ? AND provider = ?",
                (int(user_id), str(provider)),
            )

    # ---- notification email (opt-in, not identity) -------------------------
    def set_user_email(self, user_id: int, email: str) -> None:
        email = str(email or "").strip()
        with self._conn:
            self._conn.execute(
                """INSERT INTO user_emails (user_id, email, verified, updated_at)
                   VALUES (?, ?, 0, ?)
                   ON CONFLICT(user_id) DO UPDATE SET email = excluded.email, updated_at = excluded.updated_at""",
                (user_id, email, time.time()),
            )

    def get_user_email(self, user_id: int) -> str:
        row = self._conn.execute("SELECT email FROM user_emails WHERE user_id = ?", (user_id,)).fetchone()
        return row["email"] if row else ""

    # ---- background jobs ---------------------------------------------------
    def record_source_preflight(
        self,
        result: Mapping[str, Any] | Any,
        *,
        ttl_seconds: float,
    ) -> sqlite3.Row:
        data = _as_mapping(result)
        source_name = str(data.get("source_name") or data.get("source") or "").strip()
        if not source_name:
            raise ValueError("source preflight requires a source name")
        checked_at = float(data.get("checked_at") or time.time())
        expires_at = checked_at + max(1.0, float(ttl_seconds))
        with self._conn:
            self._conn.execute(
                """INSERT INTO source_preflights
                   (source_name, endpoint, request_version, checked_at, expires_at,
                    success, supported, authoritative, found, out_of,
                    returned_count, page, error, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source_name) DO UPDATE SET
                     endpoint = excluded.endpoint,
                     request_version = excluded.request_version,
                     checked_at = excluded.checked_at,
                     expires_at = excluded.expires_at,
                     success = excluded.success,
                     supported = excluded.supported,
                     authoritative = excluded.authoritative,
                     found = excluded.found,
                     out_of = excluded.out_of,
                     returned_count = excluded.returned_count,
                     page = excluded.page,
                     error = excluded.error,
                     metadata_json = excluded.metadata_json""",
                (
                    source_name,
                    str(data.get("endpoint") or ""),
                    str(data.get("request_version") or ""),
                    checked_at,
                    expires_at,
                    int(bool(data.get("success"))),
                    int(bool(data.get("supported"))),
                    int(bool(data.get("authoritative"))),
                    data.get("found"),
                    data.get("out_of"),
                    max(0, int(data.get("returned_count") or 0)),
                    data.get("page"),
                    str(data.get("error") or "")[:1000],
                    _json_text(data.get("metadata"), {}),
                ),
            )
        row = self.get_source_preflight(source_name)
        assert row is not None
        return row

    def get_source_preflight(self, source_name: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM source_preflights WHERE source_name = ?",
            (str(source_name),),
        ).fetchone()

    def create_job(
        self,
        user_id: int,
        kind: str,
        *,
        workspace_key: str = "",
        notify_email: str = "",
    ) -> sqlite3.Row:
        now = time.time()
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO background_jobs (user_id, kind, status, phase, message, error, workspace_key, notify_email, created_at, updated_at) "
                "VALUES (?, ?, 'queued', 'queued', '', '', ?, ?, ?, ?)",
                (user_id, kind, str(workspace_key or ""), str(notify_email or ""), now, now),
            )
            job_id = int(cursor.lastrowid)
        return self.get_job(job_id)

    def create_or_get_active_job(
        self,
        user_id: int,
        kind: str,
        *,
        workspace_key: str = "",
        notify_email: str = "",
    ) -> tuple[sqlite3.Row, bool]:
        """Atomically return an active equivalent job or create one."""
        key = str(workspace_key or "")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            existing = self._conn.execute(
                """SELECT * FROM background_jobs
                   WHERE user_id = ? AND kind = ? AND workspace_key = ?
                     AND status IN ('queued', 'running')
                   ORDER BY id LIMIT 1""",
                (int(user_id), str(kind), key),
            ).fetchone()
            if existing is not None:
                self._conn.commit()
                return existing, False
            now = time.time()
            cursor = self._conn.execute(
                """INSERT INTO background_jobs
                   (user_id, kind, status, phase, message, error, workspace_key,
                    notify_email, created_at, updated_at)
                   VALUES (?, ?, 'queued', 'queued', '', '', ?, ?, ?, ?)""",
                (int(user_id), str(kind), key, str(notify_email or ""), now, now),
            )
            row = self._conn.execute(
                "SELECT * FROM background_jobs WHERE id = ?",
                (int(cursor.lastrowid),),
            ).fetchone()
            self._conn.commit()
            assert row is not None
            return row, True
        except Exception:
            self._conn.rollback()
            raise

    def get_job(self, job_id: int) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM background_jobs WHERE id = ?", (int(job_id),)).fetchone()

    def list_user_jobs(self, user_id: int, limit: int = 50) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            "SELECT * FROM background_jobs WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, max(0, int(limit))),
        ))

    def get_running_jobs(self) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            "SELECT * FROM background_jobs WHERE status IN ('queued', 'running') ORDER BY id"
        ))

    def get_active_job(
        self,
        user_id: int,
        *,
        workspace_key: str,
        kind: str = "discovery",
        exclude_job_id: int | None = None,
    ) -> sqlite3.Row | None:
        sql = (
            "SELECT * FROM background_jobs WHERE user_id = ? AND kind = ? "
            "AND workspace_key = ? AND status IN ('queued', 'running')"
        )
        values: list[Any] = [int(user_id), str(kind), str(workspace_key or "")]
        if exclude_job_id is not None:
            sql += " AND id != ?"
            values.append(int(exclude_job_id))
        sql += " ORDER BY id LIMIT 1"
        return self._conn.execute(sql, values).fetchone()

    def recover_running_jobs(self) -> int:
        """Mark jobs owned by a previous server process as interrupted."""
        now = time.time()
        with self._conn:
            cursor = self._conn.execute(
                """UPDATE background_jobs
                   SET status = 'interrupted', phase = 'interrupted',
                       error = 'The server stopped while this job was running.',
                       finished_at = ?, updated_at = ?
                   WHERE status = 'running'""",
                (now, now),
            )
        return int(cursor.rowcount or 0)

    def request_job_cancel(self, job_id: int) -> sqlite3.Row | None:
        now = time.time()
        with self._conn:
            self._conn.execute(
                """UPDATE background_jobs SET cancel_requested = 1,
                   status = CASE WHEN status = 'queued' THEN 'cancelled' ELSE status END,
                   phase = CASE WHEN status = 'queued' THEN 'cancelled' ELSE phase END,
                   finished_at = CASE WHEN status = 'queued' THEN ? ELSE finished_at END,
                   updated_at = ? WHERE id = ? AND status IN ('queued', 'running')""",
                (now, now, int(job_id)),
            )
        return self.get_job(job_id)

    def update_job(
        self,
        job_id: int,
        *,
        status: str | None = None,
        phase: str | None = None,
        message: str | None = None,
        error: str | None = None,
        progress: Mapping[str, Any] | None = None,
        result: Mapping[str, Any] | None = None,
        started_at: float | None = None,
        finished_at: float | None = None,
        workspace_key: str | None = None,
        cancel_requested: bool | None = None,
        heartbeat_at: float | None = None,
    ) -> sqlite3.Row:
        fields_to_set = {
            "phase": phase,
            "message": message,
            "error": error,
            "started_at": started_at,
            "finished_at": finished_at,
            "workspace_key": workspace_key,
            "heartbeat_at": heartbeat_at,
        }
        assignments: list[str] = []
        values: list[Any] = []
        if status is not None:
            assignments.append("status = ?")
            values.append(status)
        if cancel_requested is not None:
            assignments.append("cancel_requested = ?")
            values.append(int(bool(cancel_requested)))
        for column, value in fields_to_set.items():
            if value is not None:
                if column == "started_at":
                    assignments.append("started_at = COALESCE(background_jobs.started_at, ?)")
                elif column == "finished_at":
                    assignments.append("finished_at = ?")
                else:
                    assignments.append(f"{column} = ?")
                values.append(float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else value)
        if progress is not None:
            assignments.append("progress_json = ?")
            values.append(_json_text(dict(progress), {}))
        if result is not None:
            assignments.append("result_json = ?")
            values.append(_json_text(dict(result), {}))
        assignments.append("updated_at = ?")
        values.append(time.time())
        values.append(int(job_id))
        with self._conn:
            self._conn.execute(
                f"UPDATE background_jobs SET {', '.join(assignments)} WHERE id = ?",
                values,
            )
        return self.get_job(job_id)

    def claim_job(self, job_id: int, *, started_at: float | None = None) -> sqlite3.Row | None:
        """Atomically move a queued job to running (single worker guarantee)."""
        now = time.time() if started_at is None else started_at
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE background_jobs SET status = 'running', phase = 'running', started_at = ?, heartbeat_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'queued'",
                (now, now, now, int(job_id)),
            )
            if int(cursor.rowcount or 0) == 0:
                return None
            return self._conn.execute("SELECT * FROM background_jobs WHERE id = ?", (job_id,)).fetchone()

    def delete_old_jobs(self, *, older_than_days: float = 30) -> int:
        cutoff = time.time() - float(older_than_days) * 86400
        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM background_jobs WHERE updated_at < ? AND status NOT IN ('queued', 'running')",
                (cutoff,),
            )
            return int(cursor.rowcount or 0)
