"""Manual review decisions: storage, pipeline application, web endpoints."""

import json

import pytest

from csv_to_clonehero.database import Database
from csv_to_clonehero.pipeline import run_plan, run_search
from csv_to_clonehero.review_presentation import build_review_presentation
from csv_to_clonehero.webapp import create_app

from .conftest import FakeSource, make_chart, make_track


def _presentation_candidate(*, eligibility="RECOMMENDED", decision="AUTO_ACCEPT", score=94, gates=None, quality=None, version=None):
    return {
        "eligibility": eligibility,
        "decision": decision,
        "score": score,
        "artist": "Artist",
        "title": "Song",
        "instruments": ["guitar", "bass", "drums", "vocals"],
        "diff_guitar": 3,
        "breakdown": {
            "factors": {
                "match": {
                    "title": {"similarity": 96, "exact_normalized": True},
                    "artist": {"similarity": 94, "exact_normalized": True},
                    "version_markers": version or {"track": [], "candidate": [], "common": []},
                },
                "quality": quality or {"full_band": 8, "rated_guitar": 0.5},
                "gates": gates or {
                    "hard_gate": False,
                    "playable_content": True,
                    "required_five_fret_guitar": True,
                },
            }
        },
    }


def test_review_presentation_uses_existing_evidence_for_human_language():
    result = build_review_presentation(_presentation_candidate())

    assert result["state"] == "strong"
    assert result["label"] == "Strong match"
    assert "Artist and title match exactly." in result["why"]
    assert "Guitar available." in result["why"]
    assert result["summary"] != "RECOMMENDED"
    assert any(row["value"] == "94/100" for row in result["details"])


def test_review_presentation_exposes_concern_for_review_candidate():
    result = build_review_presentation(
        _presentation_candidate(
            eligibility="ELIGIBLE_REVIEW",
            decision="REVIEW",
            score=82,
            quality={"single_instrument": -8, "unrated_guitar": -3},
            version={"track": [], "candidate": ["live"], "common": []},
        ),
        minimum_score=70,
    )

    assert result["state"] == "needs_review"
    assert result["label"] == "Needs review"
    assert "Possible live-version mismatch." in result["concerns"]
    assert "Only one instrument is available." in result["concerns"]
    assert result["selectable"] is True


def test_review_presentation_hard_gate_is_not_playable_even_with_high_score():
    result = build_review_presentation(
        _presentation_candidate(
            eligibility="INCOMPATIBLE",
            decision="REJECT",
            score=99,
            gates={
                "hard_gate": True,
                "playable_content": False,
                "required_five_fret_guitar": None,
                "hard_gate_reasons": ["no playable chart content"],
            },
        ),
        minimum_score=0,
    )

    assert result["state"] == "not_playable"
    assert result["label"] == "Not playable"
    assert result["selectable"] is False
    assert result["action"] == "Unavailable"
    assert result["why"] == ["This chart does not contain playable instrument data."]


def _seed_track(config, db, track):
    db.replace_tracks([track], {"test.csv": 1})
    return db.get_tracks()[0]


class TestDecisionsInPipeline:
    def test_manual_accept_promotes_review_candidate(self, config):
        db = Database(config.database_path)
        track = _seed_track(config, db, make_track("Low", "Flo Rida"))
        remix = make_chart("Low (Travis Barker Remix)", "Flo Rida ft. T-Pain", chart_id=1)
        source = FakeSource(song_results={("Flo Rida", "Low"): [remix]})

        from csv_to_clonehero.overrides import Overrides

        stats = run_search(config, db, [source], Overrides(), include_expansion=False)
        assert stats.review == 1  # remix alone does not auto-accept

        # Human accepts it -> next run auto-accepts.
        db.set_manual_decision(track.track_key, "test", 1, "accept")
        stats = run_search(config, db, [source], Overrides(), include_expansion=False)
        assert stats.auto == 1
        row = db.get_match_scores()[0]
        assert row["decision"] == "AUTO_ACCEPT"
        assert "manually accepted" in json.loads(row["breakdown_json"])["explanation"]
        db.close()

    def test_manual_reject_blocks_auto_candidate(self, config):
        db = Database(config.database_path)
        track = _seed_track(config, db, make_track("Everlong", "Foo Fighters"))
        good = make_chart("Everlong", "Foo Fighters", chart_id=1)
        source = FakeSource(song_results={("Foo Fighters", "Everlong"): [good]})

        from csv_to_clonehero.overrides import Overrides

        stats = run_search(config, db, [source], Overrides(), include_expansion=False)
        assert stats.auto == 1

        db.set_manual_decision(track.track_key, "test", 1, "reject")
        stats = run_search(config, db, [source], Overrides(), include_expansion=False)
        assert stats.auto == 0
        assert stats.no_match == 1
        db.close()

    def test_clear_restores_automatic_behavior(self, config):
        db = Database(config.database_path)
        track = _seed_track(config, db, make_track("Everlong", "Foo Fighters"))
        good = make_chart("Everlong", "Foo Fighters", chart_id=1)
        source = FakeSource(song_results={("Foo Fighters", "Everlong"): [good]})

        from csv_to_clonehero.overrides import Overrides

        db.set_manual_decision(track.track_key, "test", 1, "reject")
        db.set_manual_decision(track.track_key, "test", 1, "clear")
        stats = run_search(config, db, [source], Overrides(), include_expansion=False)
        assert stats.auto == 1
        db.close()


class TestReviewApi:
    @pytest.fixture
    def client(self, config):
        config.input_path.mkdir(parents=True, exist_ok=True)
        app = create_app(config, source_factory=lambda cfg: [FakeSource()])
        app.config["TESTING"] = True
        with app.test_client() as client:
            yield client, config

    def _seed_scores(self, config):
        db = Database(config.database_path)
        track = _seed_track(config, db, make_track("Low", "Flo Rida"))
        chart = make_chart("Low (Travis Barker Remix)", "Flo Rida ft. T-Pain", chart_id=1)
        db.upsert_candidate(chart)
        db.record_match_score(
            track.db_id, chart.db_id, 83.0, "REVIEW",
            json.dumps({"explanation": "title=80 artist=90 final=83.0 decision=REVIEW"}),
        )
        db.close()
        return track

    def test_review_listing_and_decide(self, client):
        http, config = client
        track = self._seed_scores(config)

        payload = http.get("/api/review").get_json()
        assert len(payload["tracks"]) == 1
        item = payload["tracks"][0]
        assert item["spotify_title"] == "Low"
        assert item["candidates"][0]["candidate_title"] == "Low (Travis Barker Remix)"
        assert item["candidates"][0]["manual"] == ""
        assert item["candidates"][0]["review"]["label"] == "Needs review"
        assert "Guitar available." in item["candidates"][0]["review"]["why"]
        assert "REVIEW" not in item["candidates"][0]["review"]["summary"]
        assert payload["progress"]["review_track_count"] == 1

        response = http.post(
            "/api/review/decide",
            json={"track_key": track.track_key, "source": "test",
                  "chart_id": 1, "decision": "accept"},
        )
        assert response.status_code == 200
        assert (
            http.get("/api/review").get_json()["tracks"][0]["candidates"][0]["manual"]
            == "accept"
        )

        http.post("/api/review/decide",
                  json={"track_key": track.track_key, "source": "test",
                        "chart_id": 1, "decision": "clear"})
        assert (
            http.get("/api/review").get_json()["tracks"][0]["candidates"][0]["manual"]
            == ""
        )

    def test_decide_validates_payload(self, client):
        http, _ = client
        assert http.post("/api/review/decide", json={"decision": "maybe"}).status_code == 400

    def test_accept_all_accepts_best_review_candidate(self, client):
        http, config = client
        track = self._seed_scores(config)

        response = http.post("/api/review/accept-all")
        assert response.status_code == 200
        assert response.get_json()["accepted"] == 1

        db = Database(config.database_path)
        assert db.get_manual_decisions()[(track.track_key, "test", 1)] == "accept"
        db.close()

    def test_grouped_alternatives_are_sorted_and_rejected_are_optional(self, client):
        http, config = client
        db = Database(config.database_path)
        track = _seed_track(config, db, make_track("Low", "Flo Rida"))
        first = make_chart("Low (Travis Barker Remix)", "Flo Rida ft. T-Pain", chart_id=1)
        second = make_chart("Low", "Flo Rida", chart_id=2)
        rejected = make_chart("Low (Live)", "Flo Rida", chart_id=3)
        for chart in (first, second, rejected):
            db.upsert_candidate(chart)
        db.record_match_score(
            track.db_id, first.db_id, 83.0, "REVIEW", json.dumps({"explanation": "first"})
        )
        db.record_match_score(
            track.db_id, second.db_id, 74.0, "REVIEW", json.dumps({"explanation": "second"})
        )
        db.record_match_score(
            track.db_id, rejected.db_id, 61.0, "REJECT", json.dumps({"explanation": "rejected"})
        )
        db.close()

        payload = http.get("/api/review").get_json()
        assert len(payload["tracks"]) == 1
        assert [c["chart_id"] for c in payload["tracks"][0]["candidates"]] == [1, 2]
        all_payload = http.get("/api/review?include_rejected=1").get_json()
        assert [c["chart_id"] for c in all_payload["tracks"][0]["candidates"]] == [1, 2, 3]

    def test_review_cards_are_server_paged_and_searchable(self, client):
        http, config = client
        db = Database(config.database_path)
        tracks = [make_track(f"Song {index:03d}", f"Artist {index:03d}") for index in range(120)]
        db.replace_tracks(tracks, {"test.csv": len(tracks)})
        for index, track in enumerate(db.get_tracks(), start=1):
            chart = make_chart(
                track.title,
                track.primary_artist,
                chart_id=index,
            )
            db.upsert_candidate(chart)
            db.record_match_score(
                track.db_id,
                chart.db_id,
                75.0,
                "REVIEW",
                json.dumps({"explanation": "review"}),
            )
        db.close()

        first = http.get(
            "/api/review",
            query_string={"limit": 10, "offset": 0, "state": "review"},
        ).get_json()
        assert len(first["tracks"]) == 10
        assert first["page"] == {
            "offset": 0,
            "limit": 10,
            "page": 1,
            "page_count": 12,
            "total": 120,
            "returned": 10,
            "has_more": True,
            "search": "",
            "state": "review",
            "sort": "artist",
        }
        assert first["progress"]["review_track_count"] == 120

        second = http.get(
            "/api/review",
            query_string={"limit": 10, "offset": 110, "state": "review"},
        ).get_json()
        assert len(second["tracks"]) == 10
        assert second["page"]["has_more"] is False
        assert second["tracks"][0]["title"] == "Song 110"

        searched = http.get(
            "/api/review",
            query_string={"limit": 50, "search": "Artist 011", "state": "eligible"},
        ).get_json()
        assert searched["page"]["total"] == 1
        assert searched["tracks"][0]["artist"] == "Artist 011"


def test_manual_accept_is_definitive_even_when_lower_scored(config):
    db = Database(config.database_path)
    track = _seed_track(config, db, make_track("Low", "Flo Rida"))
    exact = make_chart("Low", "Flo Rida", chart_id=1)
    remix = make_chart("Low (Travis Barker Remix)", "Flo Rida ft. T-Pain", chart_id=2)
    source = FakeSource(song_results={("Flo Rida", "Low"): [exact, remix]})
    from csv_to_clonehero.overrides import Overrides

    first = run_search(config, db, [source], Overrides(), include_expansion=False)
    assert first.auto == 1
    db.set_manual_decision(track.track_key, "test", 2, "accept")
    second = run_search(config, db, [source], Overrides(), include_expansion=False)
    assert second.auto == 1
    rows = {row["candidate_id"]: row for row in db.get_match_scores()}
    remix_row = rows[remix.db_id]
    exact_row = rows[exact.db_id]
    assert remix_row["decision"] == "AUTO_ACCEPT"
    assert exact_row["decision"] == "REJECT"
    assert "not selected because another candidate was manually accepted" in json.loads(
        exact_row["breakdown_json"]
    )["explanation"]
    plan = run_plan(config, db, [source], include_expansion=False)
    assert plan.exact_selected == 1
    assert db.get_selections()[0]["candidate_id"] == remix_row["candidate_id"]
    db.close()


def test_accepting_one_candidate_clears_other_accepts(config):
    db = Database(config.database_path)
    db.set_manual_decision("track", "test", 1, "accept")
    db.set_manual_decision("track", "test", 2, "reject")
    db.set_manual_decision("track", "other", 3, "accept")
    decisions = db.get_manual_decisions()
    assert ("track", "test", 1) not in decisions
    assert decisions[("track", "test", 2)] == "reject"
    assert decisions[("track", "other", 3)] == "accept"
    db.close()
