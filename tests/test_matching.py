import pytest

from csv_to_clonehero.matching import (
    ALTERNATE_RECORDING,
    BLOCKED,
    POSSIBLE_MATCH,
    RECOMMENDED_EXACT,
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


def test_same_artist_different_song_is_a_hard_title_conflict(config):
    track = make_track("The Only Exception", "Paramore")
    chart = make_chart("That's What You Get", "Paramore", charter="Harmonix", icon="rb2")
    result = score_candidate(track, chart, config)

    assert result.title_score == pytest.approx(37.8378378)
    assert result.decision == "REJECT"
    assert result.eligibility == "INCOMPATIBLE"
    assert result.final_score < config.matching.review_threshold
    assert result.gate_factors["title_conflict"] is True
    assert any("title conflict" in reason for reason in result.reasons)


def test_quality_cannot_rescue_low_title_identity(config):
    track = make_track("The Only Exception", "Paramore")
    chart = make_chart("That's What You Get", "Paramore", charter="Harmonix", icon="rb2")
    chart.pack_name = "Rock Band 2"
    chart.diff_band = 5
    chart.difficulty_fields = {"diff_band": 5, "diff_guitar": 3, "diff_bass": 3, "diff_drums": 3}
    result = score_candidate(track, chart, config)

    assert result.decision == "REJECT"
    assert result.eligibility not in {"RECOMMENDED", "ELIGIBLE_REVIEW"}
    assert result.quality == 0.0
    assert result.final_score < config.matching.review_threshold


def test_borderline_title_identity_is_never_auto_accepted_from_quality(config):
    track = make_track("Only Exception", "Paramore")
    chart = make_chart("Only One", "Paramore", charter="Harmonix", icon="rb2")
    chart.pack_name = "Official Pack"
    result = score_candidate(track, chart, config)

    assert 55.0 <= result.title_score < 70.0
    assert result.gate_factors["title_identity"] == "borderline"
    assert result.quality > 0
    assert result.final_score < config.matching.auto_accept_threshold
    assert result.decision == "REVIEW"
    assert result.eligibility == "ELIGIBLE_REVIEW"


def test_strong_normalized_title_still_auto_accepts(config):
    result = score_candidate(
        make_track("The Only Exception", "Paramore"),
        make_chart("The Only Exception", "Paramore"),
        config,
    )

    assert result.gate_factors["title_identity"] == "strong"
    assert result.match_factors["title"]["identity"]["number_word_equivalent"] is False
    assert result.decision == "AUTO_ACCEPT"
    assert result.eligibility == "RECOMMENDED"


def test_meaningful_version_mismatch_remains_version_evidence(config):
    result = score_candidate(
        make_track("Sample Song (Remix)", "Sample Artist"),
        make_chart("Sample Song", "Sample Artist"),
        config,
    )

    assert result.match_factors["title"]["identity"]["safe_identity_projection"]["matched"] is False
    assert result.gate_factors["title_conflict"] is False
    assert any("version mismatch" in label for label, _ in result.penalties)


@pytest.mark.parametrize(
    "chart_title",
    [
        "Good Ass Intro (ft. Artist One, Artist Two & Artist Three)",
        "Good Ass Intro [Overchart]",
    ],
)
def test_safe_title_projection_establishes_same_song_identity(config, chart_title):
    result = score_candidate(
        make_track("Good Ass Intro", "Chance the Rapper"),
        _high_quality_chart(chart_title, "Chance the Rapper"),
        config,
    )
    projection = result.match_factors["title"]["identity"]["safe_identity_projection"]
    assert projection["matched"] is True
    assert projection["changed"] is True
    assert result.gate_factors["title_auto_identity"] is True
    assert result.eligibility == "RECOMMENDED"


@pytest.mark.parametrize("marker", ["Live", "Remix", "Cover"])
def test_safe_title_projection_never_erases_version_identity(config, marker):
    result = score_candidate(
        make_track("Song", "Artist"),
        _high_quality_chart(f"Song ({marker})", "Artist"),
        config,
    )
    projection = result.match_factors["title"]["identity"]["safe_identity_projection"]
    assert projection["matched"] is False
    assert projection["version_markers_agree"] is False
    assert any("version mismatch" in label for label, _ in result.penalties)
    assert result.eligibility != "RECOMMENDED"


def test_result_tiers_separate_recommendation_alternate_possible_and_blocked(config):
    exact_track = make_track("Song", "Artist")
    exact_track.duration_ms = 180000
    exact = make_chart("Song", "Artist", chart_id=300)
    exact.song_length_ms = 181000
    assert score_candidate(exact_track, exact, config).result_tier == RECOMMENDED_EXACT

    alternate = make_chart("Song (Live)", "Artist", chart_id=301)
    alternate.song_length_ms = 240000
    alternate_result = score_candidate(exact_track, alternate, config)
    assert alternate_result.result_tier == ALTERNATE_RECORDING
    assert alternate_result.gate_factors["hard_gate"] is False

    possible = make_chart("Song Long", "Artist", chart_id=302)
    possible_result = score_candidate(exact_track, possible, config)
    assert possible_result.result_tier == POSSIBLE_MATCH

    blocked = make_chart("Wrong", "Unrelated", chart_id=303)
    assert score_candidate(exact_track, blocked, config).result_tier == BLOCKED


def test_pick_best_prefers_correct_song_over_higher_quality_wrong_song(config):
    track = make_track("The Only Exception", "Paramore")
    wrong = make_chart("That's What You Get", "Paramore", chart_id=1, charter="Harmonix", icon="rb2")
    wrong.pack_name = "Rock Band 2"
    right = make_chart("The Only Exception", "Paramore", chart_id=2, instruments=["guitar"])

    best, breakdown, scored = pick_best(track, [wrong, right], config)

    assert best is right
    assert breakdown.eligibility == "RECOMMENDED"
    wrong_score = next(score for chart, score in scored if chart is wrong)
    assert wrong_score.eligibility == "INCOMPATIBLE"


def _high_quality_chart(title, artist, *, chart_id=1):
    chart = make_chart(
        title, artist, chart_id=chart_id, charter="Harmonix", icon="rb2"
    )
    chart.pack_name = "Official Pack"
    chart.diff_band = 5
    chart.difficulty_fields = {
        "diff_band": 5, "diff_guitar": 3, "diff_bass": 3, "diff_drums": 3
    }
    return chart


@pytest.mark.parametrize(
    ("artist", "track_title", "chart_title"),
    [
        ("Atreyu", "Drowning", "Falling Down"),
        ("Michael Jackson", "You Rock My World", "Rock With You"),
        ("Viagra Boys", "Ain’t No Thief", "Ain’t Nice"),
        ("ONE OK ROCK", "Let Me Let You Go - Japanese Version", "Prove - Japanese version"),
    ],
)
def test_live_same_artist_false_positives_cannot_be_best_matches(
    config, artist, track_title, chart_title
):
    result = score_candidate(
        make_track(track_title, artist),
        _high_quality_chart(chart_title, artist),
        config,
    )

    assert result.gate_factors["title_auto_identity"] is False
    assert result.eligibility != "RECOMMENDED"
    assert result.decision != "AUTO_ACCEPT"
    assert result.final_score < config.matching.auto_accept_threshold


@pytest.mark.parametrize(
    ("track_title", "chart_title", "plain_ratio", "ranking_score"),
    [
        ("Drowning", "Falling Down", 40.0, 70.0),
        ("You Rock My World", "Rock With You", 46.6666667, 76.1904762),
    ],
)
def test_plain_title_conflict_floor_ignores_token_set_inflation(
    config, track_title, chart_title, plain_ratio, ranking_score
):
    result = score_candidate(
        make_track(track_title, "Same Artist"),
        _high_quality_chart(chart_title, "Same Artist"),
        config,
    )

    assert result.match_factors["title"]["plain_similarity"] == pytest.approx(plain_ratio)
    assert result.title_score == pytest.approx(ranking_score)
    assert result.gate_factors["title_identity"] == "conflict"
    assert result.eligibility == "INCOMPATIBLE"


def test_plain_artist_conflict_floor_ignores_token_set_inflation(config):
    result = score_candidate(
        make_track("You", "Daryl Hall & John Oates"),
        _high_quality_chart("You", "Tally Hall"),
        config,
    )

    artist = result.match_factors["artist"]
    assert artist["plain_similarity"] < config.matching.artist_conflict_threshold
    assert result.artist_score >= config.matching.artist_conflict_threshold
    assert result.gate_factors["artist_identity"] == "conflict"
    assert result.eligibility == "INCOMPATIBLE"


@pytest.mark.parametrize(
    ("track_artist", "track_title", "chart_artist", "chart_title"),
    [
        ("Daryl Hall & John Oates", "You Make My Dreams (Come True)", "Tally Hall", "You"),
        ("Tyler, The Creator", "911 / Mr. Lonely (feat. Frank Ocean & Steve Lacy)", "The Bunny the Bear", "Lonely"),
        ("Keyshia Cole", "Love", "Kesha", "Dirty Love (feat. Iggy Pop)"),
        ("Machine Girl", "Ghost", "Rage Against The Machine", "The Ghost Of Tom Joad"),
    ],
)
def test_live_cross_artist_false_positives_cannot_be_best_matches(
    config, track_artist, track_title, chart_artist, chart_title
):
    result = score_candidate(
        make_track(track_title, track_artist),
        _high_quality_chart(chart_title, chart_artist),
        config,
    )

    assert result.eligibility != "RECOMMENDED"
    assert result.decision != "AUTO_ACCEPT"
    assert not (
        result.gate_factors["title_auto_identity"]
        and result.gate_factors["artist_auto_identity"]
    )


@pytest.mark.parametrize(
    ("track_title", "chart_title"),
    [("Song One", "Song 1"), ("Song 1", "Song One")],
)
def test_number_word_equivalent_titles_auto_identify_in_both_directions(
    config, track_title, chart_title
):
    result = score_candidate(
        make_track(track_title, "Sample Artist"),
        _high_quality_chart(chart_title, "Sample Artist"),
        config,
    )

    identity = result.match_factors["title"]["identity"]
    assert identity["number_word_equivalent"] is True
    assert result.eligibility == "RECOMMENDED"


@pytest.mark.parametrize(
    ("artist", "track_title", "chart_artist", "chart_title"),
    [
        ("B.o.B", "Airplanes (feat. Hayley Williams)", "B.o.B featuring Hayley Williams", "Airplanes"),
        ("Bee Gees", "Stayin' Alive - From Saturday Night Fever Soundtrack", "The Bee Gees", "Stayin' Alive"),
        ("Yasuha", "Fly-day Chinatown", "Yasuha", "Flyday Chinatown"),
        ("Dexys Midnight Runners", "Come On Eileen", "Dexys Midnight Runners", "Come on Elieen"),
        ("The Weeknd", "Pray For Me", "The Weeknd & Kendrick Lamar", "Pray for Me"),
    ],
)
def test_legitimate_nonexact_title_and_credit_variants_remain_auto_identified(
    config, artist, track_title, chart_artist, chart_title
):
    result = score_candidate(
        make_track(track_title, artist),
        _high_quality_chart(chart_title, chart_artist),
        config,
    )

    assert result.gate_factors["title_auto_identity"] is True
    assert result.gate_factors["artist_auto_identity"] is True
    assert result.eligibility == "RECOMMENDED"


def test_title_auto_artist_auto_allows_quality_recommendation(config):
    result = score_candidate(
        make_track("Sample Song", "Sample Artist"),
        _high_quality_chart("Sample Song", "Sample Artist"),
        config,
    )
    assert result.eligibility == "RECOMMENDED"


def test_title_review_artist_auto_never_recommended(config):
    result = score_candidate(
        make_track("Ain’t No Thief", "Viagra Boys"),
        _high_quality_chart("Ain’t Nice", "Viagra Boys"),
        config,
    )
    assert result.gate_factors["title_identity"] == "borderline"
    assert result.gate_factors["artist_identity"] == "strong"
    assert result.eligibility == "ELIGIBLE_REVIEW"


def test_title_auto_artist_review_never_recommended(config):
    result = score_candidate(
        make_track("Wake Up", "Ahmad Anonimis"),
        _high_quality_chart("Wake Up", "Mad Season"),
        config,
    )
    assert result.gate_factors["title_identity"] == "strong"
    assert result.gate_factors["artist_identity"] == "borderline"
    assert result.eligibility == "ELIGIBLE_REVIEW"


def test_both_review_identities_never_recommended(config):
    result = score_candidate(
        make_track("Only Exception", "Ahmad Anonimis"),
        _high_quality_chart("Only One", "Mad Season"),
        config,
    )
    assert result.gate_factors["title_identity"] == "borderline"
    assert result.gate_factors["artist_identity"] == "borderline"
    assert result.eligibility == "ELIGIBLE_REVIEW"


def test_exact_isrc_identity_establishes_pair_despite_unusual_text(config):
    track = make_track("You Make My Dreams (Come True)", "Daryl Hall & John Oates")
    track.isrc = "US-AAA-12-34567"
    chart = _high_quality_chart("You", "Tally Hall")
    chart.source_metadata = {"isrc": "USAAA1234567"}
    result = score_candidate(track, chart, config)

    assert result.gate_factors["external_identity_match"] is True
    assert result.gate_factors["title_auto_identity"] is True
    assert result.gate_factors["artist_auto_identity"] is True
    assert result.eligibility == "RECOMMENDED"


def test_exact_spotify_identity_establishes_pair(config):
    track = make_track(
        "You Make My Dreams (Come True)", "Daryl Hall & John Oates",
        spotify_id="spotify-track-123",
    )
    chart = _high_quality_chart("You", "Tally Hall")
    chart.source_metadata = {"spotify": {"track_id": "spotify-track-123"}}
    result = score_candidate(track, chart, config)

    assert result.gate_factors["external_identity_match"] is True
    assert result.eligibility == "RECOMMENDED"


def test_track_isrc_without_candidate_is_unknown_not_conflict(config):
    track = make_track("Sample Song", "Sample Artist")
    track.isrc = "US-AAA-12-34567"
    result = score_candidate(
        track,
        _high_quality_chart("Sample Song", "Sample Artist"),
        config,
    )

    assert result.match_factors["isrc"]["match"] is None
    assert result.gate_factors["external_identity_conflict"] is False
    assert result.eligibility == "RECOMMENDED"


def test_track_spotify_id_without_candidate_is_unknown_not_conflict(config):
    track = make_track("Sample Song", "Sample Artist", spotify_id="track-one")
    result = score_candidate(
        track,
        _high_quality_chart("Sample Song", "Sample Artist"),
        config,
    )

    assert result.match_factors["spotify_identity"]["exact_match"] is None
    assert result.gate_factors["external_identity_conflict"] is False
    assert result.eligibility == "RECOMMENDED"


def test_isrc_mismatch_alone_is_hard_gate(config):
    track = make_track("Sample Song", "Sample Artist")
    track.isrc = "US-AAA-12-34567"
    chart = _high_quality_chart("Sample Song", "Sample Artist")
    chart.source_metadata = {"isrc": "US-BBB-12-34567"}
    result = score_candidate(track, chart, config)

    assert result.match_factors["isrc"]["match"] is False
    assert result.gate_factors["external_identity_conflict"] is True
    assert result.eligibility == "INCOMPATIBLE"


def test_spotify_mismatch_alone_is_hard_gate(config):
    track = make_track("Sample Song", "Sample Artist", spotify_id="track-one")
    chart = _high_quality_chart("Sample Song", "Sample Artist")
    chart.source_metadata = {"spotify": {"track_id": "track-two"}}
    result = score_candidate(track, chart, config)

    assert result.match_factors["spotify_identity"]["exact_match"] is False
    assert result.gate_factors["external_identity_conflict"] is True
    assert result.eligibility == "INCOMPATIBLE"


def test_external_identity_mismatch_is_hard_gate(config):
    track = make_track("Sample Song", "Sample Artist", spotify_id="track-one")
    track.isrc = "US-AAA-12-34567"
    chart = _high_quality_chart("Sample Song", "Sample Artist")
    chart.source_metadata = {
        "isrc": "US-BBB-12-34567",
        "spotify": {"track_id": "track-two"},
    }
    result = score_candidate(track, chart, config)

    assert result.gate_factors["external_identity_conflict"] is True
    assert result.eligibility == "INCOMPATIBLE"
    assert result.decision == "REJECT"
    assert any("external song identity conflict" in reason for reason in result.reasons)


def test_huge_quality_cannot_rescue_review_artist_identity(config):
    result = score_candidate(
        make_track("Wake Up", "Ahmad Anonimis"),
        _high_quality_chart("Wake Up", "Mad Season"),
        config,
    )
    assert result.quality > 0
    assert result.final_score < config.matching.auto_accept_threshold
    assert result.eligibility == "ELIGIBLE_REVIEW"


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
