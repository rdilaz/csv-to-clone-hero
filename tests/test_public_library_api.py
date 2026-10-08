"""Public imported-library and catalog-count contracts, without provider calls."""

from __future__ import annotations

import pytest

from csv_to_clonehero.config import CATALOG_PATH_ENV
from csv_to_clonehero.database import Database
from csv_to_clonehero.providers.base import LibrarySnapshot, ProviderTrack
from csv_to_clonehero.server.safeguards import get_discovery_availability
from tests.conftest import make_chart
from tests.test_catalog import candidate, write_snapshot
from tests.test_server_web import _sign_in, app, client, config, spotify_client_id


def _import_library(config, workspace_id, *, prefix="Imported", count=63, status="complete"):
    db = Database(config.database_path)
    try:
        db.upsert_library_snapshot(
            workspace_id,
            LibrarySnapshot("spotify", [
                ProviderTrack(
                    "spotify", f"{prefix.casefold()}-{index}",
                    f"{prefix} Song {index:03d}", f"{prefix} Artist",
                )
                for index in range(count)
            ]),
        )
        tracks = sorted(
            db.get_library_tracks(workspace_id=workspace_id), key=lambda track: track.title
        )
        db.update_workspace(workspace_id, status=status, phase="complete")
        return tracks
    finally:
        db.close()


@pytest.mark.parametrize(
    ("status", "empty_state"), [("complete", "no_match"), ("partial", "incomplete")]
)
def test_full_imported_library_survives_matching_filters_and_pagination(
    client, config, status, empty_state
):
    _sign_in(client)
    workspace_id = client.get("/api/journey").get_json()["workspace_id"]
    tracks = _import_library(config, workspace_id, status=status)
    db = Database(config.database_path)
    try:
        chart = make_chart(tracks[0].title, tracks[0].artist_full, chart_id=9100)
        db.upsert_workspace_candidate(
            workspace_id, tracks[0].track_key, chart, score=99,
            eligibility="RECOMMENDED", decision="AUTO_ACCEPT", kind="exact",
        )
    finally:
        db.close()

    all_cards = []
    summaries = []
    for offset, expected_page_size in [(0, 25), (25, 25), (50, 13)]:
        response = client.get("/api/workspace", query_string={"limit": 25, "offset": offset})
        assert response.status_code == 200
        page = response.get_json()
        assert page["total"] == 63
        assert page["summary"]["total"] == 63
        assert page["summary"]["recommended_songs"] == 1
        assert len(page["tracks"]) == expected_page_size
        summaries.append(page["summary"])
        all_cards.extend(page["tracks"])

    assert {card["track_key"] for card in all_cards} == {track.track_key for track in tracks}
    assert len({card["track_key"] for card in all_cards}) == len(all_cards) == 63
    unmatched = [card for card in all_cards if not card["candidates"]]
    assert len(unmatched) == 62
    assert {card["review_state"] for card in unmatched} == {empty_state}
    assert summaries[0] == summaries[1] == summaries[2]

    recommended = client.get(
        "/api/workspace", query_string={"tier": "RECOMMENDED_EXACT", "limit": 25}
    ).get_json()
    assert recommended["total"] == 1
    assert [card["track_key"] for card in recommended["tracks"]] == [tracks[0].track_key]
    assert recommended["summary"] == summaries[0]

    searched = client.get(
        "/api/workspace", query_string={"search": "Imported Song 062", "limit": 25}
    ).get_json()
    assert searched["total"] == 1
    assert searched["tracks"][0]["title"] == "Imported Song 062"
    assert searched["tracks"][0]["candidates"] == []
    assert searched["summary"] == summaries[0]


def test_unfiltered_imported_library_is_private_to_the_authenticated_owner(
    app, client, config, monkeypatch
):
    import csv_to_clonehero.server.web as web

    anonymous = app.test_client()
    assert anonymous.get("/api/workspace").status_code == 401

    _sign_in(client)
    alice_workspace_id = client.get("/api/journey").get_json()["workspace_id"]
    _import_library(config, alice_workspace_id, prefix="Alice Private", count=2)

    monkeypatch.setattr(
        web, "_fetch_profile",
        lambda cfg, token: {"id": "user-2", "display_name": "Bob", "country": "US"},
    )
    bob = app.test_client()
    _sign_in(bob)
    bob_workspace_id = bob.get("/api/journey").get_json()["workspace_id"]
    assert bob_workspace_id != alice_workspace_id
    _import_library(config, bob_workspace_id, prefix="Bob Private", count=3)

    for owner, own_prefix, other_prefix, own_workspace, other_workspace, count in [
        (client, "Alice Private", "Bob Private", alice_workspace_id, bob_workspace_id, 2),
        (bob, "Bob Private", "Alice Private", bob_workspace_id, alice_workspace_id, 3),
    ]:
        response = owner.get(
            "/api/workspace", query_string={"workspace_id": other_workspace, "limit": 100}
        )
        assert response.status_code == 200
        library = response.get_json()
        assert library["workspace"] == own_workspace
        assert library["total"] == library["summary"]["total"] == count
        assert len(library["tracks"]) == count
        assert all(card["title"].startswith(own_prefix) for card in library["tracks"])
        assert all(not card["candidates"] for card in library["tracks"])

        hidden = owner.get("/api/workspace", query_string={"search": other_prefix}).get_json()
        assert hidden["tracks"] == []
        assert hidden["total"] == 0
        assert hidden["summary"]["total"] == count


def test_catalog_counts_distinguish_known_records_eligible_charts_and_unique_songs(
    config, monkeypatch
):
    monkeypatch.delenv(CATALOG_PATH_ENV, raising=False)
    config.paths.catalog = "catalog.jsonl"
    write_snapshot(config.catalog_path, [
        candidate(1, "Artist", "Song"),
        candidate(2, "ARTIST", "SONG"),
        candidate(3, "Other Artist", "Other Song"),
    ])
    db = Database(config.database_path)
    try:
        db.record_chart_artifact_validation(
            "enchor", 3, state="REJECTED", reason_code="synthetic_invalid_chart"
        )
        availability = get_discovery_availability(config, db)
    finally:
        db.close()

    assert availability["mode"] == "catalog"
    assert availability["usable"] is True
    assert availability["out_of"] == 3
    assert availability["eligible_charts"] == 2
    assert availability["eligible_songs"] == 1
