from csv_to_clonehero.matching import (
    instrument_compatible,
    pick_best,
    score_candidate,
    score_expansion_candidate,
)

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


# ------------------------------------------------------------- quality gates
def test_no_content_chart_hard_rejected_exact(config):
    track = make_track("Song", "Artist")
    chart = make_chart("Song", "Artist", instruments=[], diff_guitar=None)
    result = score_candidate(track, chart, config)
    assert result.decision == "REJECT"
    assert result.final_score == 0.0
    assert any("no playable chart content" in r for r in result.reasons)


def test_no_content_chart_hard_rejected_expansion(config):
    chart = make_chart("Song", "Artist", instruments=[], diff_guitar=None)
    result = score_expansion_candidate("artist", chart, config)
    assert result.decision == "REJECT"
    assert any("no playable chart content" in r for r in result.reasons)


def test_difficulty_only_chart_has_content(config):
    # No instrument list but a rated guitar difficulty counts as content.
    track = make_track("Song", "Artist")
    chart = make_chart("Song", "Artist", instruments=[], diff_guitar=4)
    result = score_candidate(track, chart, config)
    assert result.decision != "REJECT" or not any(
        "no playable chart content" in r for r in result.reasons
    )


def test_single_instrument_penalized_below_full_band(config):
    # Imperfect title similarity keeps both scores below the 100 cap so the
    # quality gap is observable in final_score.
    track = make_track("Song One", "Artist")
    guitar_only = make_chart("Song 1", "Artist", chart_id=1, instruments=["guitar"])
    full_band = make_chart("Song 1", "Artist", chart_id=2)
    guitar_score = score_candidate(track, guitar_only, config)
    band_score = score_candidate(track, full_band, config)
    assert band_score.final_score > guitar_score.final_score
    assert any(
        "single-instrument" in label for label, _ in guitar_score.penalties
    )
    # The full-band chart is the planner's winner for the same song.
    best, breakdown, _ = pick_best(track, [guitar_only, full_band], config)
    assert best is full_band
    assert breakdown.decision == "AUTO_ACCEPT"


def test_unrated_guitar_difficulty_penalized(config):
    # Featured-artist credit keeps scores below the cap so the small penalty
    # is observable in final_score.
    track = make_track("Song One", "Artist")
    unrated = make_chart("Song 1", "Artist ft. Guest", chart_id=1, diff_guitar=None)
    rated = make_chart("Song 1", "Artist ft. Guest", chart_id=2, diff_guitar=3)
    unrated_score = score_candidate(track, unrated, config)
    rated_score = score_candidate(track, rated, config)
    assert any(
        "guitar difficulty unrated" in label for label, _ in unrated_score.penalties
    )
    assert rated_score.final_score > unrated_score.final_score


def test_has_issues_penalty_increased(config):
    track = make_track("Song", "Artist")
    chart = make_chart("Song", "Artist")
    chart.has_issues = True
    result = score_candidate(track, chart, config)
    assert ("chart/metadata issues reported", 10) in result.penalties


def test_expansion_full_band_beats_single_instrument(config):
    guitar_only = make_chart("Song", "Artist", chart_id=1, instruments=["guitar"])
    full_band = make_chart("Song", "Artist", chart_id=2)
    single = score_expansion_candidate("artist", guitar_only, config)
    band = score_expansion_candidate("artist", full_band, config)
    assert any("single-instrument" in label for label, _ in single.penalties)
    assert band.final_score > single.final_score
