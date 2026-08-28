"""Multi-source plumbing: several ChartSources feed one pipeline.

The planner picks the highest-scoring candidate per track regardless of which
source offered it, and the download goes through the winning source. Charts
from different sources are never deduped away from each other (they are
distinct candidates), and unknown source names fail loudly.
"""

import hashlib

import pytest

from csv_to_clonehero.database import Database
from csv_to_clonehero.pipeline import run_downloads, run_plan, run_search
from csv_to_clonehero.sources import build_sources

from .conftest import FakeSource, make_chart, make_track, valid_sng_bytes


class _NamedSource(FakeSource):
    def __init__(self, name, **kwargs):
        super().__init__(**kwargs)
        self.name = name


def _build_two_sources():
    content_a = valid_sng_bytes("Song A")
    content_b = valid_sng_bytes("Song B")
    md5_a = hashlib.md5(content_a).hexdigest()
    md5_b = hashlib.md5(content_b).hexdigest()
    # Source A: single-instrument, unrated chart of the same song.
    chart_a = make_chart(
        "Song", "Artist", chart_id=1, md5=md5_a,
        instruments=["guitar"], diff_guitar=None,
    )
    # Source B: full-band official conversion of the same song.
    chart_b = make_chart(
        "Song", "Artist", chart_id=2, md5=md5_b, icon="rb3",
    )
    source_a = _NamedSource(
        "source_a",
        song_results={("Artist", "Song"): [chart_a]},
        files={md5_a: content_a},
    )
    source_b = _NamedSource(
        "source_b",
        song_results={("Artist", "Song"): [chart_b]},
        files={md5_b: content_b},
    )
    return source_a, source_b, chart_b, md5_b


def test_highest_quality_chart_wins_across_sources(config):
    db = Database(config.database_path)
    track = make_track("Song", "Artist")
    db.replace_tracks([track], {"test.csv": 1})
    source_a, source_b, chart_b, _ = _build_two_sources()

    from csv_to_clonehero.overrides import Overrides

    stats = run_search(config, db, [source_a, source_b], Overrides(),
                       include_expansion=False)
    assert stats.auto == 1

    # Both candidates were scored (no cross-source title dedupe)...
    rows = db.get_match_scores()
    assert len(rows) == 2
    # ...but the full-band official chart from source B wins the plan.
    run_plan(config, db, [source_a, source_b], include_expansion=False)
    selections = db.get_selections(status="selected")
    assert len(selections) == 1
    winner = db.get_candidate(selections[0]["candidate_id"])
    assert winner.source == "source_b"
    assert winner.chart_id == chart_b.chart_id

    # And the download goes through the winning source only.
    downloaded, errors, _ = run_downloads(config, db, [source_a, source_b])
    assert (downloaded, errors) == (1, 0)
    assert source_a.downloads == []
    assert len(source_b.downloads) == 1
    db.close()


def test_unknown_source_name_rejected(config):
    from csv_to_clonehero.config import SourcesConfig

    config.sources = SourcesConfig(enabled=["nope"])
    with pytest.raises(ValueError, match="Unknown chart source"):
        build_sources(config)
