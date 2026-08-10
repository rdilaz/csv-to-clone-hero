"""Manual review decisions: storage, pipeline application, web endpoints."""

import io
import json
import time

import pytest

from csv_to_clonehero.database import Database
from csv_to_clonehero.pipeline import run_search
from csv_to_clonehero.webapp import create_app

from .conftest import FIXTURES, FakeSource, make_chart, make_track


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

        stats = run_search(config, db, [source], Overrides())
        assert stats.review == 1  # remix alone does not auto-accept

        # Human accepts it -> next run auto-accepts.
        db.set_manual_decision(track.track_key, "test", 1, "accept")
        stats = run_search(config, db, [source], Overrides())
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

        stats = run_search(config, db, [source], Overrides())
        assert stats.auto == 1

        db.set_manual_decision(track.track_key, "test", 1, "reject")
        stats = run_search(config, db, [source], Overrides())
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
        stats = run_search(config, db, [source], Overrides())
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

        items = http.get("/api/review").get_json()
        assert len(items) == 1
        item = items[0]
        assert item["spotify_title"] == "Low"
        assert item["candidate_title"] == "Low (Travis Barker Remix)"
        assert item["manual"] == ""

        response = http.post(
            "/api/review/decide",
            json={"track_key": track.track_key, "source": "test",
                  "chart_id": 1, "decision": "accept"},
        )
        assert response.status_code == 200
        assert http.get("/api/review").get_json()[0]["manual"] == "accept"

        http.post("/api/review/decide",
                  json={"track_key": track.track_key, "source": "test",
                        "chart_id": 1, "decision": "clear"})
        assert http.get("/api/review").get_json()[0]["manual"] == ""

    def test_decide_validates_payload(self, client):
        http, _ = client
        assert http.post("/api/review/decide", json={"decision": "maybe"}).status_code == 400
