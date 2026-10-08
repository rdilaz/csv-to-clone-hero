import hashlib
import json
import os
from dataclasses import asdict

from csv_to_clonehero.catalog import CatalogLoadError, EnchorCatalog, export_catalog, load_enchor_catalog
from csv_to_clonehero.models import ChartCandidate
from csv_to_clonehero.normalization import normalize_artist, normalize_title


def candidate(chart_id, artist, title):
    return ChartCandidate(
        source="enchor", chart_id=chart_id, md5=str(chart_id), name=title,
        artist=artist, name_norm=title.casefold(), artist_norm=artist.casefold(),
        instruments=["guitar"],
    )


def write_snapshot(path, candidates):
    body = "".join(
        json.dumps(
            {"type": "chart", "candidate": asdict(value)},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
        for value in candidates
    ).encode()
    header = {
        "type": "enchor-catalog",
        "format_version": 1,
        "complete": True,
        "snapshot_id": "fixture",
        "chart_count": len(candidates),
        "out_of": len(candidates),
        "sha256": hashlib.sha256(body).hexdigest(),
    }
    path.write_text(
        json.dumps(header, sort_keys=True, separators=(",", ":"))
        + "\n"
        + body.decode(),
        encoding="utf-8",
    )


class FakeSource:
    def __init__(self):
        self.pages = {
            1: [candidate(1, "Artist", "Song")],
            2: [candidate(2, "Artist", "Song")],
        }

    @staticmethod
    def _response_total(response):
        return response.get("out_of")

    @staticmethod
    def _parse_chart(item):
        return item

    def _post(self, _path, payload):
        page = payload["page"]
        return {"found": 2, "out_of": 2, "page": page, "data": self.pages.get(page, [])}


def test_export_loads_duplicate_chart_versions(tmp_path):
    path = tmp_path / "catalog.jsonl"
    export_catalog(FakeSource(), path, page_size=1)
    catalog = EnchorCatalog(path)
    assert catalog.available
    assert catalog.has_artist("artist")
    assert catalog.has_song("Artist", "Song")
    assert len(catalog.candidates("Artist", "Song")) == 2


def test_incomplete_snapshot_is_not_authoritative(tmp_path):
    path = tmp_path / "catalog.jsonl"
    path.write_text(json.dumps({"type": "enchor-catalog", "format_version": 1, "complete": False}) + "\n")
    catalog = EnchorCatalog(path)
    assert not catalog.available
    assert not catalog.has_song("Artist", "Song")


def test_catalog_header_count_mismatch_is_not_authoritative(tmp_path):
    path = tmp_path / "catalog.jsonl"
    write_snapshot(path, [candidate(1, "Artist", "Song")])
    lines = path.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    header["chart_count"] = 2
    path.write_text(
        json.dumps(header, sort_keys=True, separators=(",", ":"))
        + "\n"
        + "\n".join(lines[1:])
        + "\n",
        encoding="utf-8",
    )
    catalog = EnchorCatalog(path)
    assert not catalog.available
    assert "chart_count" in catalog.diagnostic


def test_export_rejects_unstable_page_total(tmp_path):
    class Bad(FakeSource):
        def _post(self, path, payload):
            result = super()._post(path, payload)
            if payload["page"] == 2:
                result["out_of"] = 3
            return result

    try:
        export_catalog(Bad(), tmp_path / "catalog.jsonl", page_size=1)
    except CatalogLoadError as exc:
        assert "unstable" in str(exc)
    else:
        raise AssertionError("inconsistent totals must not produce a snapshot")


def test_export_resumes_from_durable_page_checkpoint(tmp_path):
    destination = tmp_path / "catalog.jsonl"
    checkpoint = tmp_path / "checkpoint.json"
    partial = tmp_path / "catalog.jsonl.partial"
    row = {"type": "chart", "candidate": candidate(1, "Artist", "Song").__dict__}
    partial.write_text(json.dumps(row) + "\n", encoding="utf-8")
    checkpoint.write_text(json.dumps({"page": 2, "total": 2, "ids": [1]}), encoding="utf-8")
    result = export_catalog(FakeSource(), destination, checkpoint=checkpoint, page_size=1)
    assert result["chart_count"] == 2
    assert EnchorCatalog(destination).available


def test_high_recall_lookup_keeps_versions_and_strict_key_semantics(tmp_path):
    path = tmp_path / "catalog.jsonl"
    write_snapshot(
        path,
        [
            candidate(1, "Sample Artist (WaveGroup)", "Sample Song"),
            candidate(2, "Sample Artist (WaveGroup)", "Sample Song (Live)"),
        ],
    )
    catalog = EnchorCatalog(path)

    assert not catalog.has_song("Sample Artist", "Sample Song")
    lookup = catalog.high_recall_candidates(
        "Sample Artist",
        "Sample Song",
        artist_fuzzy_limit=0,
        title_fuzzy_limit=0,
    )
    assert lookup.strict_exact is False
    assert lookup.exact_artist is True
    assert lookup.exact_title is True
    assert {item.chart_id for item in lookup.candidates} == {1, 2}


def test_high_recall_lookup_handles_featured_artist_and_title_aliases(tmp_path):
    path = tmp_path / "catalog.jsonl"
    write_snapshot(path, [candidate(10, "Sample Artist", "Sample Song (Live)")])
    catalog = EnchorCatalog(path)

    lookup = catalog.high_recall_candidates(
        "Sample Artist feat. Guest",
        "Sample Song",
        alternate_artists=("Sample Artist",),
        artist_fuzzy_limit=0,
        title_fuzzy_limit=0,
    )
    assert lookup.candidates[0].chart_id == 10
    assert "title" in lookup.methods or "artist" in lookup.methods


def test_high_recall_lookup_can_use_bounded_fuzzy_title_alias(tmp_path):
    path = tmp_path / "catalog.jsonl"
    write_snapshot(path, [candidate(11, "Sample Artist", "Song")])
    catalog = EnchorCatalog(path)
    lookup = catalog.high_recall_candidates(
        "Sample Artst",
        "Sogn",
        artist_fuzzy_limit=5,
        title_fuzzy_limit=5,
        min_title_similarity=60,
    )
    assert [item.chart_id for item in lookup.candidates] == [11]
    assert {"artist_fuzzy", "title_fuzzy"}.issubset(lookup.methods)


def test_staged_lookup_excludes_unrelated_same_artist_songs(tmp_path):
    path = tmp_path / "catalog.jsonl"
    write_snapshot(
        path,
        [
            candidate(20, "Chance the Rapper", "Good Ass Intro"),
            candidate(21, "Chance the Rapper", "Cocoa Butter Kisses"),
            candidate(22, "Chance the Rapper", "Chain Reaction"),
        ],
    )
    lookup = EnchorCatalog(path).high_recall_candidates(
        "Chance The Rapper", "Chain Smoker"
    )
    assert {item.name for item in lookup.candidates} == {"Chain Reaction"}


def test_staged_lookup_protects_symbolic_artist_collisions(tmp_path):
    path = tmp_path / "catalog.jsonl"
    write_snapshot(
        path,
        [
            candidate(30, "$NOT", "Revenge"),
            candidate(31, "Snot", "Revenge"),
        ],
    )
    catalog = EnchorCatalog(path)
    assert [item.chart_id for item in catalog.high_recall_candidates("$NOT", "Revenge").candidates] == [30]
    assert [item.chart_id for item in catalog.high_recall_candidates("Snot", "Revenge").candidates] == [31]


def test_staged_lookup_bounds_generic_titles_and_short_artists(tmp_path):
    path = tmp_path / "catalog.jsonl"
    rows = [candidate(40, "X", "Love")]
    rows.extend(candidate(41 + index, f"Other {index}", "Love") for index in range(30))
    write_snapshot(path, rows)
    lookup = EnchorCatalog(path).high_recall_candidates(
        "X", "Love", song_key_limit=3, candidate_limit=4
    )
    assert [item.chart_id for item in lookup.candidates] == [40]


def test_staged_lookup_uses_bounded_alternate_artist_credit(tmp_path):
    path = tmp_path / "catalog.jsonl"
    write_snapshot(
        path,
        [
            candidate(80, "Featured Artist", "Shared Title"),
            candidate(81, "Unrelated Artist", "Shared Title"),
        ],
    )
    lookup = EnchorCatalog(path).high_recall_candidates(
        "Uploader", "Shared Title", alternate_artists=("Featured Artist",)
    )
    assert [item.chart_id for item in lookup.candidates] == [80]


def test_artist_songs_uses_indexed_artist_pool(tmp_path):
    path = tmp_path / "catalog.jsonl"
    write_snapshot(
        path,
        [candidate(1, "Artist", "First"), candidate(2, "Other", "First")],
    )
    catalog = EnchorCatalog(path)
    assert [(title, [row.chart_id for row in rows]) for title, rows in catalog.artist_songs("Artist")] == [
        (normalize_title("First"), [1])
    ]


def test_same_path_snapshot_replacement_refreshes_cache_identity(tmp_path):
    path = tmp_path / "catalog.jsonl"
    write_snapshot(path, [candidate(1, "Artist", "First")])
    first = load_enchor_catalog(path)
    write_snapshot(path, [candidate(2, "Artist", "Other")])
    second = load_enchor_catalog(path)
    assert second is not first
    assert first.sha256 != second.sha256
    assert second.has_chart(2)
    assert not second.has_chart(1)


def test_catalog_cache_retains_only_current_full_catalog(tmp_path):
    path = tmp_path / "catalog.jsonl"
    write_snapshot(path, [candidate(1, "Artist", "First")])
    load_enchor_catalog.cache_clear()

    first = load_enchor_catalog(path, validation_fingerprint="ledger-a")
    eligibility_changed = load_enchor_catalog(
        path,
        excluded_identities={("enchor", 1)},
        validation_fingerprint="ledger-b",
    )
    assert eligibility_changed is not first
    assert not eligibility_changed.has_chart(1)
    assert load_enchor_catalog.cache_info().currsize == 1

    write_snapshot(path, [candidate(2, "Artist", "Second")])
    snapshot_changed = load_enchor_catalog(
        path, validation_fingerprint="ledger-b"
    )
    assert snapshot_changed is not eligibility_changed
    assert snapshot_changed.has_chart(2)
    assert load_enchor_catalog.cache_info().currsize == 1


def test_snapshot_cache_uses_header_when_size_and_timestamp_are_identical(tmp_path):
    path = tmp_path / "catalog.jsonl"
    write_snapshot(path, [candidate(1, "Artist", "First")])
    original_stat = path.stat()
    first = load_enchor_catalog(path)
    write_snapshot(path, [candidate(2, "Artist", "Other")])
    assert path.stat().st_size == original_stat.st_size
    os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    second = load_enchor_catalog(path)

    assert second is not first
    assert second.available and second.has_chart(2) and not second.has_chart(1)
    assert load_enchor_catalog(path) is second


def test_snapshot_cache_recovers_from_corrupt_header_with_identical_stat(tmp_path):
    path = tmp_path / "catalog.jsonl"
    write_snapshot(path, [candidate(1, "Artist", "First")])
    original_stat, valid = path.stat(), path.read_bytes()
    first = load_enchor_catalog(path)
    path.write_bytes(b"!" + valid[1:])
    os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    invalid = load_enchor_catalog(path)

    assert invalid is not first and not invalid.available
    path.write_bytes(valid)
    os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert load_enchor_catalog(path).available


def test_zero_body_catalog_with_oversized_valid_header_is_unavailable(tmp_path):
    path = tmp_path / "catalog.jsonl"
    write_snapshot(path, [])
    header = json.loads(path.read_text(encoding="utf-8"))
    header["padding"] = " " * (64 * 1024)
    path.write_text(json.dumps(header) + "\n", encoding="utf-8")

    invalid = load_enchor_catalog(path)

    assert not invalid.available
    assert "header is too large" in invalid.diagnostic


def test_warm_snapshot_cache_reads_only_bounded_header(tmp_path, monkeypatch):
    from pathlib import Path

    path = tmp_path / "catalog.jsonl"
    write_snapshot(path, [candidate(1, "Artist", "First")])
    first = load_enchor_catalog(path)
    real_open = Path.open
    header_reads = []

    class HeaderOnlyStream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def readline(self, limit=-1):
            assert 0 < limit <= 64 * 1024
            header_reads.append(limit)
            return self.stream.readline(limit)

        def read(self, *args):
            raise AssertionError("warm cache must not read the full catalog body")

    def header_only_open(self, *args, **kwargs):
        stream = real_open(self, *args, **kwargs)
        return HeaderOnlyStream(stream) if self == path else stream

    monkeypatch.setattr(Path, "open", header_only_open)

    assert load_enchor_catalog(path) is first
    assert len(header_reads) == 1
