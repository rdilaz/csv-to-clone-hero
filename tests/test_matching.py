from csv_to_clonehero.matching import instrument_compatible, pick_best, score_candidate

from .conftest import make_chart, make_track


def test_artist_conflict_rejected(config):
    track = make_track("Low", "Flo Rida")
    chart = make_chart("Low", "Nirvana")
    result = score_candidate(track, chart, config)
    assert result.decision == "REJECT"
    assert result.final_score < config.matching.review_threshold
    assert any("artist conflict" in r for r in result.reasons)


def test_remaster_matches_chart_strongly(config):
    track = make_track("Paint It, Black - 2009 Remaster", "The Rolling Stones")
    chart = make_chart("Paint It, Black", "The Rolling Stones")
    result = score_candidate(track, chart, config)
    assert result.decision == "AUTO_ACCEPT"
    assert result.final_score >= config.matching.auto_accept_threshold


def test_everlong_remaster_matches(config):
    track = make_track("Everlong - 2015 Remaster", "Foo Fighters")
    chart = make_chart("Everlong", "Foo Fighters")
    result = score_candidate(track, chart, config)
    assert result.decision == "AUTO_ACCEPT"


def test_drums_only_chart_incompatible(config):
    track = make_track("Song", "Artist")
    chart = make_chart("Song", "Artist", instruments=["drums"], diff_guitar=None)
    result = score_candidate(track, chart, config)
    assert result.decision == "INCOMPATIBLE"
    compatible, _ = instrument_compatible(chart, config)
    assert not compatible


def test_six_fret_only_rejected_by_default(config):
    track = make_track("Song", "Artist")
    chart = make_chart("Song", "Artist", instruments=["guitarghl"], diff_guitar=None)
    result = score_candidate(track, chart, config)
    assert result.decision == "INCOMPATIBLE"


def test_modchart_excluded(config):
    track = make_track("Song", "Artist")
    chart = make_chart("Song", "Artist", modchart=True)
    result = score_candidate(track, chart, config)
    assert result.decision == "INCOMPATIBLE"


def test_cover_version_penalized(config):
    track = make_track("Song", "Artist")
    cover = make_chart("Song (Cover)", "Artist")
    original = make_chart("Song", "Artist", chart_id=2)
    cover_score = score_candidate(track, cover, config)
    original_score = score_candidate(track, original, config)
    assert original_score.final_score > cover_score.final_score


def test_pick_best_prefers_correct_artist(config):
    track = make_track("Low", "Flo Rida")
    wrong = make_chart("Low", "Nirvana", chart_id=1)
    right = make_chart("Low", "Flo Rida", chart_id=2)
    best, breakdown, scored = pick_best(track, [wrong, right], config)
    assert best is right
    assert breakdown.decision == "AUTO_ACCEPT"
    assert len(scored) == 2


def test_score_explanation_present(config):
    track = make_track("Paint It, Black - 2009 Remaster", "The Rolling Stones")
    chart = make_chart("Paint It, Black", "The Rolling Stones")
    result = score_candidate(track, chart, config)
    explanation = result.explain()
    assert "title=" in explanation
    assert "artist=" in explanation
    assert "final=" in explanation
    assert "decision=" in explanation
