import hashlib
import json
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
    assert "title_fuzzy" in lookup.methods


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
