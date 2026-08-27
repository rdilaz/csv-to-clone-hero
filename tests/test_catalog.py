import json

from csv_to_clonehero.catalog import CatalogLoadError, EnchorCatalog, export_catalog
from csv_to_clonehero.models import ChartCandidate


def candidate(chart_id, artist, title):
    return ChartCandidate(
        source="enchor", chart_id=chart_id, md5=str(chart_id), name=title,
        artist=artist, name_norm=title.casefold(), artist_norm=artist.casefold(),
        instruments=["guitar"],
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
