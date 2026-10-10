"""Studio selection spans all pages while retaining owner and identity gates."""
import json

import pytest

from csv_to_clonehero.catalog_validation import VALIDATOR_VERSION
from csv_to_clonehero.database import Database
from csv_to_clonehero.server import web
from tests.conftest import make_chart, make_track
from tests.test_server_web import app, client, config, spotify_client_id, _sign_in


def _seed(client, config, *, count=2, variants=2):
    _sign_in(client)
    client.get("/api/sources")
    db = Database(config.database_path)
    workspace = int(db.get_workspace(Database.workspace_key_for_user("user-1"))["id"])
    tracks = [make_track(f"Song {index:04}", "Artist", spotify_id=f"{index:022}") for index in range(count)]
    db.replace_tracks(tracks, {"fixture.csv": count})
    tracks = db.get_spotify_tracks(True)
    db.replace_workspace_library_tracks(workspace, tracks)
    generation = db.create_discovery_scan(workspace, scan_key="studio-exact", kind="exact", status="complete")
    db.update_workspace(workspace, status="complete", phase="complete")
    task = db.upsert_discovery_task(workspace, scan_id=generation, source="test", query_key="all", status="complete")
    ids = []
    for index, track in enumerate(tracks):
        for variant in range(variants):
            chart = make_chart(track.title, track.artist_full, chart_id=1000 + index * variants + variant)
            db.upsert_workspace_candidate(workspace, track.track_key, chart,
                score=99, eligibility="RECOMMENDED", decision="AUTO_ACCEPT", task_id=task)
            ids.append(chart.db_id)
    extra = make_chart("Additional Song", "Artist", chart_id=70000, instruments=["guitar"])
    db.upsert_workspace_candidate(workspace, None, extra, kind="expansion", artist_norm="artist",
        score=50, eligibility="ELIGIBLE_REVIEW", decision="REVIEW",
        metadata={"exact_generation_id": generation, "coverage_mode": "all"})
    wrong = make_chart("Mad Man", "Haste the Day", chart_id=80000)
    db.upsert_workspace_candidate(workspace, tracks[0].track_key, wrong,
        score=99, eligibility="RECOMMENDED", decision="AUTO_ACCEPT", task_id=task)
    db.close()
    return workspace, generation, tracks, ids, extra.db_id, wrong.db_id


def test_select_all_crosses_pages_prefers_verified_multiple_and_preserves_other_scope(client, config):
    workspace, generation, tracks, ids, extra, wrong = _seed(client, config, count=125)
    db = Database(config.database_path)
    multiple = db.get_candidate(ids[1])
    notes = {"guitar:easy": 2, "guitar:hard": 4, "guitar:expert": 8}
    db.record_chart_artifact_validation(multiple.source, multiple.chart_id,
        artifact_ref=multiple.md5, state="VERIFIED", validator_version=VALIDATOR_VERSION,
        artifact_sha256="a" * 64, playable_note_count=14,
        validation={"valid": True, "validation_version": VALIDATOR_VERSION,
                    "note_counts": notes, "playable_note_count": 14})
    db.select_workspace_candidate(workspace, None, extra, kind="expansion", artist_norm="artist", force=True,
                                  metadata={"possible_confirmed": True})
    db.close()
    response = client.post("/api/selection/all", json={"scope": "library"})
    assert response.status_code == 200, response.get_json()
    state = response.get_json()
    assert state["summary"] == {"library": {"songs": 125, "charts": 125}, "expansion": {"songs": 1, "charts": 1}}
    library = state["selected"]["library"]
    assert any(item["candidate_id"] == ids[1] for item in library)
    assert all(item["candidate_id"] != wrong for item in library)
    assert state["selected"]["expansion"][0]["candidate_id"] == extra
    assert client.post("/api/selection/all", json={"scope": "library"}).get_json()["changed"] == 0
    all_versions = client.post("/api/selection/all", json={"scope": "both", "policy": "all_versions", "confirm_possible": True}).get_json()
    assert all_versions["summary"]["library"] == {"songs": 125, "charts": 250}
    assert all_versions["summary"]["expansion"] == {"songs": 1, "charts": 1}


def test_listening_deck_spans_all_pages_no_guessed_expansion_ids_and_no_provider_calls(client, app, config):
    workspace, generation, tracks, ids, extra, wrong = _seed(client, config, count=121)
    app.extensions["jobs"].source_factory = lambda _: pytest.fail("deck and all-selection never query providers")
    first = client.get("/api/listening-deck?limit=100").get_json()
    assert first["total"] == 122 and len(first["items"]) == 100 and first["has_more"]
    assert first["exact_generation_id"] == generation
    token = first["generation_token"]
    second = client.get(f"/api/listening-deck?limit=100&offset=100&generation_token={token}").get_json()
    assert len(second["items"]) == 22 and not second["has_more"]
    values = first["items"] + second["items"]
    assert len({item["key"] for item in values}) == 122
    for item in values:
        assert item["candidate_id"] != wrong and item["selectable"]
        if item["scope"] == "library":
            assert len(item["spotify_id"]) == 22
            assert item["spotify_url"] == f'https://open.spotify.com/track/{item["spotify_id"]}'
            assert item["chart_title"] == item["title"]
        else:
            assert item["spotify_id"] is None and item["spotify_url"] is None
    selected = client.post("/api/selection/all", json={"scope": "both", "confirm_possible": True})
    assert selected.status_code == 200
    assert selected.get_json()["charts"] == 122
    filtered = client.get("/api/listening-deck?q=Additional%20Song").get_json()
    assert filtered["total"] == 1 and filtered["items"][0]["selected"]


@pytest.mark.parametrize("payload", [
    {"scope": "expansion", "confirm_possible": "true"},
    {"scope": []}, {"policy": "all"}, ["bad"],
])
def test_selection_rejects_ambiguous_input(client, config, payload):
    _seed(client, config)
    assert client.post("/api/selection/all", json=payload).status_code == 400


def test_current_generation_owner_and_active_jobs_gate_studio(client, app, config, monkeypatch):
    workspace, generation, tracks, ids, extra, wrong = _seed(client, config)
    anonymous = app.test_client()
    assert anonymous.get("/api/listening-deck").status_code == 401
    assert anonymous.post("/api/selection/all", json={}).status_code == 401
    db = Database(config.database_path)
    old = client.get("/api/listening-deck").get_json()["generation_token"]
    db.create_discovery_scan(workspace, scan_key="new-run", kind="exact", status="running")
    db.close()
    assert client.post("/api/selection/all", json={}).status_code == 409
    assert client.get(f"/api/listening-deck?generation_token={old}").status_code == 409
    assert client.get("/api/listening-deck").get_json()["total"] == 0
    monkeypatch.setattr(Database, "get_active_job", lambda *args, **kwargs: {"id": 10})
    assert client.post("/api/selection/all", json={}).status_code == 409


def test_note_rejection_and_chart_metadata_change_invalidate_warm_score_cache(client, config):
    workspace, generation, tracks, ids, extra, wrong = _seed(client, config, count=1, variants=1)
    assert client.get("/api/listening-deck?scope=library").get_json()["total"] == 1
    db = Database(config.database_path)
    candidate = db.get_candidate(ids[0])
    candidate.modchart = True
    db.upsert_candidate(candidate)
    db.close()
    assert client.get("/api/listening-deck?scope=library").get_json()["total"] == 0
    db = Database(config.database_path)
    candidate.modchart = False
    db.upsert_candidate(candidate)
    db.record_chart_artifact_validation(candidate.source, candidate.chart_id,
        artifact_ref=candidate.md5, state="REJECTED", reason="no actual notes")
    db.close()
    assert client.get("/api/listening-deck?scope=library").get_json()["total"] == 0
    assert client.post("/api/selection/all", json={"scope": "library"}).status_code == 400


def test_selection_transaction_rolls_back_both_scopes_on_write_failure(client, config, monkeypatch):
    workspace, generation, tracks, ids, extra, wrong = _seed(client, config)
    assert client.post("/api/selection/all", json={"scope": "both", "confirm_possible": True}).status_code == 200
    db = Database(config.database_path)
    before = db.public_selection_state(workspace)
    db.close()
    original = Database.replace_public_selection
    def fail_after_write(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise ValueError("fixture transaction failure")
    monkeypatch.setattr(Database, "replace_public_selection", fail_after_write)
    assert client.post("/api/selection/all", json={"scope": "library"}).status_code == 400
    db = Database(config.database_path)
    assert db.public_selection_state(workspace) == before
    db.close()


def test_studio_capabilities_and_embed_policy_are_public(client):
    health = client.get("/healthz")
    assert health.get_json()["ui_release"] == "riff-ux-20261010"
    assert health.get_json()["capabilities"]["select_all"]
    policy = health.headers["Content-Security-Policy"]
    assert "frame-src 'self' https://open.spotify.com;" in policy


def test_lazy_seeded_shuffle_is_stable_unique_cached_and_selection_fresh(client, config, monkeypatch):
    workspace, generation, tracks, ids, extra, wrong = _seed(client, config, count=135, variants=1)
    first = client.get("/api/listening-deck?order=shuffle&seed=0123456789abcdef&limit=100").get_json()
    token = first["generation_token"]
    assert first["order"] == "shuffle" and first["seed"] == "0123456789abcdef"
    monkeypatch.setattr(web, "_listening_deck_items", lambda *args: pytest.fail("unchanged deck page must reuse cached identity and note metadata"))
    second = client.get(f"/api/listening-deck?order=shuffle&seed=0123456789abcdef&limit=100&offset=100&generation_token={token}").get_json()
    all_keys = [item["key"] for item in first["items"] + second["items"]]
    assert len(all_keys) == len(set(all_keys)) == 136
    repeated = client.get(f"/api/listening-deck?order=shuffle&seed=0123456789abcdef&limit=100&generation_token={token}").get_json()
    assert [item["key"] for item in repeated["items"]] == all_keys[:100]
    assert client.post("/api/selection/all", json={"scope": "both", "confirm_possible": True}).status_code == 200
    after = client.get(f"/api/listening-deck?order=shuffle&seed=0123456789abcdef&limit=100&generation_token={token}").get_json()
    assert all(item["selected"] for item in after["items"])
    assert after["generation_token"] == token


@pytest.mark.parametrize("query", ["order=bad", "order=shuffle", "order=shuffle&seed=" + "a" * 65, "limit=bad"])
def test_deck_rejects_bad_shuffle_and_paging(client, config, query):
    _seed(client, config)
    assert client.get("/api/listening-deck?" + query).status_code == 400


def test_database_owned_revision_observes_direct_sql_edits_and_config_changes(client, config):
    workspace, generation, tracks, ids, extra, wrong = _seed(client, config, count=1, variants=1)
    before = client.get("/api/listening-deck?scope=library").get_json()
    db = Database(config.database_path)
    revision = db.public_review_revision()
    row = db._conn.execute("SELECT data_json FROM candidate_charts WHERE id=?", (ids[0],)).fetchone()
    data = json.loads(row["data_json"])
    data["modchart"] = True
    with db._conn:
        db._conn.execute("UPDATE candidate_charts SET data_json=? WHERE id=?", (json.dumps(data), ids[0]))
    assert db.public_review_revision() > revision
    db.close()
    assert client.get(f'/api/listening-deck?scope=library&generation_token={before["generation_token"]}').status_code == 409
    assert client.get("/api/listening-deck?scope=library").get_json()["total"] == 0
    config.instruments.exclude_modcharts = False
    assert client.get("/api/listening-deck?scope=library").get_json()["total"] == 1


def test_schema20_upgrade_preserves_library_and_selected_charts(client, config):
    workspace, generation, tracks, ids, extra, wrong = _seed(client, config)
    assert client.post("/api/selection/all", json={"scope": "both", "confirm_possible": True}).status_code == 200
    db = Database(config.database_path)
    before = db.public_selection_state(workspace)
    with db._conn:
        for row in db._conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'studio_review_%'").fetchall():
            db._conn.execute(f'DROP TRIGGER "{row["name"]}"')
        db._conn.execute("DROP TABLE public_review_clock")
        db._conn.execute("UPDATE schema_version SET version=20")
    db.close()
    upgraded = Database(config.database_path)
    assert upgraded.public_selection_state(workspace) == before
    assert upgraded.get_workspace_library_track_ids(workspace) == {track.db_id for track in tracks}
    assert upgraded.public_review_revision() == 0
    upgraded.close()


def test_deck_and_selection_are_reciprocally_owner_scoped(client, app, config, monkeypatch):
    workspace, generation, tracks, ids, extra, wrong = _seed(client, config)
    monkeypatch.setattr(web, "_fetch_profile", lambda *args: {"id": "user-2", "display_name": "Bob", "country": "US", "product": "premium", "href": ""})
    other = app.test_client()
    _sign_in(other)
    other.get("/api/sources")
    db = Database(config.database_path)
    other_workspace = int(db.get_workspace(Database.workspace_key_for_user("user-2"))["id"])
    second_tracks = db.upsert_spotify_tracks([make_track("Private song", "Second Artist", spotify_id="b" * 22)])
    db.replace_workspace_library_tracks(other_workspace, second_tracks)
    other_generation = db.create_discovery_scan(other_workspace, scan_key="private", kind="exact", status="complete")
    task = db.upsert_discovery_task(other_workspace, scan_id=other_generation, source="test", query_key="private", status="complete")
    chart = make_chart("Private song", "Second Artist", chart_id=90000)
    db.upsert_workspace_candidate(other_workspace, second_tracks[0].track_key, chart,
        score=99, eligibility="RECOMMENDED", decision="AUTO_ACCEPT", task_id=task)
    db.close()
    alice = client.get("/api/listening-deck").get_json()
    bob = other.get("/api/listening-deck").get_json()
    assert alice["total"] == 3 and bob["total"] == 1
    assert chart.db_id not in {item["candidate_id"] for item in alice["items"]}
    assert bob["items"][0]["candidate_id"] == chart.db_id
    selected = other.post("/api/selection/all", json={"scope": "library"}).get_json()
    assert selected["charts"] == 1 and selected["selected"]["library"][0]["candidate_id"] == chart.db_id
    assert client.post("/api/selection", json={"track_key": second_tracks[0].track_key, "candidate_id": chart.db_id}).status_code == 400


def test_single_selection_returns_authoritative_counts_and_selected_versions(client, app, config):
    workspace, generation, tracks, ids, extra, wrong = _seed(client, config, count=1)
    app.extensions["jobs"].source_factory = lambda _: []
    selected = client.post("/api/selection", json={"track_key": tracks[0].track_key, "candidate_id": ids[1]}).get_json()
    assert selected["summary"]["library"] == {"songs": 1, "charts": 1}
    assert selected["selected"]["library"] == [{"track_key": tracks[0].track_key, "candidate_id": ids[1]}]
    deck = client.get("/api/listening-deck?scope=library").get_json()
    assert deck["items"][0]["selected"] and deck["items"][0]["candidate_id"] == ids[1]


def _isolated_deck(config, title):
    db = Database(config.database_path)
    workspace = db.create_workspace(workspace_key="same-numeric-workspace", status="complete")
    db.replace_tracks([make_track(title, "Private Artist", spotify_id="z" * 22)], {"fixture.csv": 1})
    track = db.get_spotify_tracks(True)[0]
    db.replace_workspace_library_tracks(workspace, [track])
    generation = db.create_discovery_scan(workspace, scan_key="same-numeric-generation", kind="exact", status="complete")
    task = db.upsert_discovery_task(workspace, scan_id=generation, source="test", query_key="one", status="complete")
    db.upsert_workspace_candidate(workspace, track.track_key, make_chart(title, "Private Artist"),
        score=99, eligibility="RECOMMENDED", decision="AUTO_ACCEPT", task_id=task)
    with db._conn:
        db._conn.execute("UPDATE public_review_clock SET revision=42 WHERE id=1")
    return db, workspace, generation


def test_cache_namespace_is_database_identity_not_numeric_owner_ids(config, tmp_path):
    first_config = config.model_copy(deep=True)
    second_config = config.model_copy(deep=True)
    first_config.base_dir = tmp_path / "first"
    second_config.base_dir = tmp_path / "second"
    assert first_config.model_dump() == second_config.model_dump()  # base_dir is excluded
    first, owner, generation = _isolated_deck(first_config, "First private song")
    second, other_owner, other_generation = _isolated_deck(second_config, "Second private song")
    assert owner == other_owner and generation == other_generation
    token_a, items_a = web._cached_listening_deck(first_config, first, owner, "library", generation)
    token_b, items_b = web._cached_listening_deck(second_config, second, other_owner, "library", other_generation)
    assert items_a[0]["title"] == "First private song"
    assert items_b[0]["title"] == "Second private song"
    assert token_a != token_b
    first.close()
    second.close()


def test_same_path_database_replacement_invalidates_cached_deck(config):
    first, owner, generation = _isolated_deck(config, "Old private song")
    token_a, _ = web._cached_listening_deck(config, first, owner, "library", generation)
    original_identity = config.database_path.stat().st_ino
    first.close()
    config.database_path.rename(config.database_path.with_suffix(".preserved.db"))
    fresh, fresh_owner, fresh_generation = _isolated_deck(config, "Restored private song")
    assert config.database_path.stat().st_ino != original_identity
    token_b, items = web._cached_listening_deck(config, fresh, fresh_owner, "library", fresh_generation)
    assert items[0]["title"] == "Restored private song" and token_b != token_a
    fresh.close()


@pytest.mark.parametrize("kind", ["discovery", "expansion", "download"])
def test_single_selection_busy_job_guard_preserves_existing_choices(client, app, config, monkeypatch, kind):
    workspace, generation, tracks, ids, extra, wrong = _seed(client, config, count=1)
    app.extensions["jobs"].source_factory = lambda _: []
    selected = client.post("/api/selection", json={"track_key": tracks[0].track_key, "candidate_id": ids[0]})
    assert selected.status_code == 200
    db = Database(config.database_path)
    before = db.public_selection_state(workspace)
    db.close()
    monkeypatch.setattr(Database, "get_active_job", lambda *args, **kwargs: {"id": 10} if kwargs.get("kind") == kind else None)
    for action in ("select", "deselect"):
        response = client.post("/api/selection", json={"track_key": tracks[0].track_key, "candidate_id": ids[1], "action": action})
        assert response.status_code == 409
    db = Database(config.database_path)
    assert db.public_selection_state(workspace) == before
    db.close()


@pytest.mark.parametrize("scope", ["library", "expansion"])
def test_song_level_selected_state_includes_other_versions_in_both_scopes(client, app, config, scope):
    workspace, generation, tracks, ids, extra, wrong = _seed(client, config, count=1)
    app.extensions["jobs"].source_factory = lambda _: []
    db = Database(config.database_path)
    alternative = make_chart("Additional Song", "Artist", chart_id=70001, instruments=["guitar"])
    db.upsert_workspace_candidate(workspace, None, alternative, kind="expansion", artist_norm="artist",
        score=50, eligibility="ELIGIBLE_REVIEW", decision="REVIEW",
        metadata={"exact_generation_id": generation, "coverage_mode": "all"})
    db.close()
    before = client.get("/api/listening-deck?scope=" + scope).get_json()
    assert not before["items"][0]["song_selected"]
    assert not before["items"][0]["selected"]
    candidate_id = ids[1] if scope == "library" else alternative.db_id
    payload = {"candidate_id": candidate_id, "kind": "exact" if scope == "library" else "expansion", "confirm_possible": True}
    if scope == "library":
        payload["track_key"] = tracks[0].track_key
    else:
        payload["artist_norm"] = "artist"
    chosen = client.post("/api/selection", json=payload)
    assert chosen.status_code == 200, chosen.get_json()
    state = chosen.get_json()
    if scope == "expansion":
        row = state["selected"]["expansion"][0]
        assert row["song_key"] == before["items"][0]["song_key"]
        assert before["items"][0]["key"] == "expansion:" + row["song_key"]
    token = before["generation_token"]
    after = client.get(f"/api/listening-deck?scope={scope}&generation_token={token}").get_json()
    assert after["generation_token"] == token  # selection overlays do not rebuild the deck
    assert after["items"][0]["song_selected"]
    assert after["items"][0]["selected"] and after["items"][0]["candidate_id"] == candidate_id
    # The cached suggested candidate remains independently unselected while
    # song_selected is true, as used by clients that retain their initial page.
    db = Database(config.database_path)
    _, cached = web._cached_listening_deck(config, db, workspace, scope, generation)
    selected = db.public_selection_state(workspace)
    db.close()
    suggested = dict(cached[0], _versions=[cached[0]["_versions"][0]])
    patched = web._deck_selection_overlay([suggested], selected)[0]
    assert patched["song_selected"] and not patched["selected"]
