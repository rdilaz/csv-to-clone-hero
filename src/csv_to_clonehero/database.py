"""SQLite persistent state: tracks, cached searches, candidates, scores,
selections and downloads. Enables caching, resume and idempotent rebuilds.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

from csv_to_clonehero.models import ChartCandidate, Track

SCHEMA_VERSION = 1

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
    ignored INTEGER NOT NULL DEFAULT 0
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
    error TEXT
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
"""


def _candidate_to_json(candidate: ChartCandidate) -> str:
    data = {
        k: v
        for k, v in vars(candidate).items()
        if k != "db_id"
    }
    return json.dumps(data)


def _candidate_from_json(db_id: int, text: str) -> ChartCandidate:
    data = json.loads(text)
    data.pop("db_id", None)
    candidate = ChartCandidate(**data)
    candidate.db_id = db_id
    return candidate


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    def _migrate(self) -> None:
        with self._conn:
            self._conn.executescript(SCHEMA)
            row = self._conn.execute("SELECT COUNT(*) AS c FROM schema_version").fetchone()
            if row["c"] == 0:
                self._conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
                )

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
    def upsert_candidate(self, candidate: ChartCandidate) -> ChartCandidate:
        with self._conn:
            self._conn.execute(
                """INSERT INTO candidate_charts (source, chart_id, md5, data_json)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(source, chart_id) DO UPDATE SET
                       md5 = excluded.md5, data_json = excluded.data_json""",
                (
                    candidate.source,
                    candidate.chart_id,
                    candidate.md5,
                    _candidate_to_json(candidate),
                ),
            )
            row = self._conn.execute(
                "SELECT id FROM candidate_charts WHERE source = ? AND chart_id = ?",
                (candidate.source, candidate.chart_id),
            ).fetchone()
        candidate.db_id = row["id"]
        return candidate

    def get_candidate(self, db_id: int) -> ChartCandidate | None:
        row = self._conn.execute(
            "SELECT id, data_json FROM candidate_charts WHERE id = ?", (db_id,)
        ).fetchone()
        return _candidate_from_json(row["id"], row["data_json"]) if row else None

    # -------------------------------------------------------- search caching
    @staticmethod
    def query_key(kind: str, *parts: str) -> str:
        raw = json.dumps([kind, *parts])
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def get_cached_query(self, query_key: str, ttl_days: int) -> tuple[list[ChartCandidate], str | None] | None:
        """Return (candidates, error) if a fresh cached query exists."""
        row = self._conn.execute(
            "SELECT id, searched_at, error FROM search_queries WHERE query_key = ?",
            (query_key,),
        ).fetchone()
        if row is None:
            return None
        if ttl_days >= 0 and time.time() - row["searched_at"] > ttl_days * 86400:
            return None
        candidates = [
            self.get_candidate(cid)
            for cid in self._query_candidate_ids(row["id"])
        ]
        return [c for c in candidates if c is not None], row["error"]

    def record_query(
        self,
        query_key: str,
        kind: str,
        query_text: str,
        candidates: list[ChartCandidate],
        error: str | None = None,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """INSERT INTO search_queries (query_key, kind, query_text, searched_at, result_count, error)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(query_key) DO UPDATE SET
                       searched_at = excluded.searched_at,
                       result_count = excluded.result_count,
                       error = excluded.error""",
                (query_key, kind, query_text, time.time(), len(candidates), error),
            )
            row = self._conn.execute(
                "SELECT id FROM search_queries WHERE query_key = ?", (query_key,)
            ).fetchone()
            query_id = row["id"]
            self._conn.execute(
                "DELETE FROM search_query_candidates WHERE query_id = ?", (query_id,)
            )
            for candidate in candidates:
                if candidate.db_id is None:
                    raise ValueError("candidate must be upserted before record_query")
                self._conn.execute(
                    "INSERT OR IGNORE INTO search_query_candidates (query_id, candidate_id) VALUES (?, ?)",
                    (query_id, candidate.db_id),
                )

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
    ) -> None:
        with self._conn:
            self._conn.execute(
                """INSERT INTO downloads (candidate_id, status, bytes, path, md5, downloaded_at, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(candidate_id) DO UPDATE SET
                       status = excluded.status, bytes = excluded.bytes, path = excluded.path,
                       md5 = excluded.md5, downloaded_at = excluded.downloaded_at,
                       error = excluded.error""",
                (candidate_id, status, bytes_count, path, md5, time.time(), error),
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
        with self._conn:
            if decision == "clear":
                self._conn.execute(
                    "DELETE FROM manual_decisions WHERE track_key = ? AND source = ? AND chart_id = ?",
                    (track_key, source, chart_id),
                )
            else:
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

    # ----------------------------------------------------------------- errors
    def record_error(self, stage: str, message: str, detail: str = "") -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO errors (stage, message, detail, occurred_at) VALUES (?, ?, ?, ?)",
                (stage, message, detail, time.time()),
            )

    def get_errors(self) -> list[sqlite3.Row]:
        return list(self._conn.execute("SELECT * FROM errors ORDER BY occurred_at"))
