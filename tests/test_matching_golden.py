from __future__ import annotations

import json
import hashlib
from dataclasses import asdict
from pathlib import Path

from csv_to_clonehero.catalog import EnchorCatalog
from csv_to_clonehero.config import AppConfig
from csv_to_clonehero.matching import score_candidate
from csv_to_clonehero.models import ChartCandidate
from csv_to_clonehero.normalization import normalize_artist, normalize_title

from .conftest import make_track


GOLDEN = Path(__file__).parent / "fixtures" / "matching_golden.json"


def _chart(chart_id: int, artist: str, title: str, *, instruments=None):
    return ChartCandidate(
        source="enchor",
        chart_id=chart_id,
        md5=f"{chart_id:032d}",
        name=title,
        artist=artist,
        name_norm=normalize_title(title),
        artist_norm=normalize_artist(artist),
        instruments=instruments if instruments is not None else ["guitar", "bass"],
        diff_guitar=3 if instruments != ["drums"] else None,
    )


def _write_catalog(path: Path, charts: list[ChartCandidate]) -> None:
    body = "".join(
        json.dumps({"type": "chart", "candidate": asdict(chart)}, sort_keys=True, separators=(",", ":"))
        + "\n"
        for chart in charts
    ).encode()
    header = {
        "type": "enchor-catalog",
        "format_version": 1,
        "complete": True,
        "snapshot_id": "golden",
        "chart_count": len(charts),
        "out_of": len(charts),
        "sha256": hashlib.sha256(body).hexdigest(),
    }
    path.write_bytes(
        (json.dumps(header, sort_keys=True, separators=(",", ":")) + "\n").encode() + body
    )


def test_golden_cases_keep_retrieval_and_acceptance_separate(tmp_path, config):
    cases = json.loads(GOLDEN.read_text(encoding="utf-8"))
    charts = []
    for index, case in enumerate(cases, start=1):
        if case["chart_artist"]:
            charts.append(
                _chart(
                    index,
                    case["chart_artist"],
                    case["chart_title"],
                    instruments=["drums"] if case["acceptance"] == "HARD_INCOMPATIBLE" else None,
                )
            )
    catalog_path = tmp_path / "golden.jsonl"
    _write_catalog(catalog_path, charts)
    catalog = EnchorCatalog(catalog_path)
    cfg = AppConfig()

    for case in cases:
        track = make_track(case["track_title"], case["track_artist"])
        alternate = ("Sample Artist",) if case["id"] == "uploader_identity" else ()
        lookup = catalog.high_recall_candidates(
            track.artist_full,
            track.title,
            alternate_artists=alternate,
            artist_fuzzy_limit=0,
            title_fuzzy_limit=0,
        )
        if case["id"] == "unrelated_title":
            result = score_candidate(
                track,
                _chart(100, case["chart_artist"], case["chart_title"]),
                cfg,
            )
            assert result.decision == "REJECT"
            continue
        elif case["id"] == "catalog_miss":
            assert not lookup.candidates
        else:
            assert lookup.candidates, case["id"]

        if case["id"] == "uploader_identity":
            assert score_candidate(track, lookup.candidates[0], cfg).decision == "REJECT"
            continue
        if case["id"] in {"unrelated_title", "catalog_miss"}:
            continue
        selected_chart = lookup.candidates[0]
        if case["acceptance"] == "HARD_INCOMPATIBLE":
            selected_chart = next(
                item for item in lookup.candidates if item.instruments == ["drums"]
            )
        result = score_candidate(track, selected_chart, cfg)
        if case["acceptance"] == "HIGH":
            assert result.decision == "AUTO_ACCEPT", case["id"]
        elif case["acceptance"] == "REVIEW":
            assert result.decision in {"AUTO_ACCEPT", "REVIEW"}
        else:
            assert result.decision == "INCOMPATIBLE"
