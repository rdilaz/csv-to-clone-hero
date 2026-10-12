"""Actual-note evidence survives validation, public review, and ZIP assembly."""

from __future__ import annotations

import hashlib
import io
import json
import struct
import zipfile
from dataclasses import asdict

import pytest

from csv_to_clonehero.catalog_validation import inspected_difficulty_coverage
from csv_to_clonehero.database import Database
from csv_to_clonehero.exporter import SNG_VALIDATION_VERSION, validate_sng
from csv_to_clonehero.providers.base import LibrarySnapshot, ProviderTrack
from csv_to_clonehero.server.downloads import DownloadError, _validated_selections, build_archive
from tests.conftest import FakeSource, make_chart
from tests.test_server_web import _sign_in, app, client, config, spotify_client_id


def _package(data, member="notes.chart"):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("song.ini", "[song]\nname = Fixture\n")
        archive.writestr(member, data)
    return payload.getvalue()


def _chart(*sections):
    return _package("[Song]\n{\nResolution = 192\n}\n" + "".join(
        f"[{name}]\n{{\n" + "".join(f"{index * 192} = N {fret} 0\n" for index, fret in enumerate(frets)) + "}\n"
        for name, frets in sections
    ))


def _midi(track_name, notes, *, enhanced_opens=False):
    name = track_name.encode()
    events = b"\x00\xff\x03" + bytes([len(name)]) + name
    if enhanced_opens:
        text = b"[ENHANCED_OPENS]"
        events += b"\x00\xff\x01" + bytes([len(text)]) + text
    for note in notes:
        events += b"\x00\x90" + bytes([note, 100]) + b"\x0a\x80" + bytes([note, 0])
    events += b"\x00\xff\x2f\x00"
    data = b"MThd" + struct.pack(">IHHH", 6, 1, 1, 480) + b"MTrk" + struct.pack(">I", len(events)) + events
    return _package(data, "notes.mid")


@pytest.mark.parametrize("frets", [[5], [6], [5, 6]])
def test_text_guitar_modifiers_alone_are_not_playable_notes(tmp_path, frets):
    path = tmp_path / "modifiers.sng"
    path.write_bytes(_chart(("ExpertSingle", frets)))
    assert not validate_sng(path, require_five_fret_guitar=True).valid


@pytest.mark.parametrize("note", [58, 65, 66, 69, 70, 77, 78, 81, 82, 89, 90, 93, 94, 101, 102, 103, 116])
def test_midi_guitar_non_lane_notes_never_prove_playability(tmp_path, note):
    path = tmp_path / "marker.sng"
    path.write_bytes(_midi("PART GUITAR", [note]))
    assert not validate_sng(path, require_five_fret_guitar=True).valid


@pytest.mark.parametrize("name", ["PART REAL_GUITAR", "PART REAL_GUITAR_22", "not a guitar", "EVENTS"])
def test_midi_pro_guitar_or_unrecognized_tracks_are_not_five_fret(tmp_path, name):
    path = tmp_path / "other.sng"
    path.write_bytes(_midi(name, [96]))
    assert not validate_sng(path, require_five_fret_guitar=True).valid


@pytest.mark.parametrize("name", ["PART GUITAR", "T1 GEMS"])
def test_midi_real_lanes_keep_each_difficulty_and_legacy_guitar(tmp_path, name):
    path = tmp_path / "levels.sng"
    path.write_bytes(_midi(name, [60, 72, 84, 96, 101, 102]))
    result = validate_sng(path, require_five_fret_guitar=True)
    assert result.valid, result.error
    assert dict(result.note_counts) == {f"guitar:{level}": 1 for level in ["easy", "medium", "hard", "expert"]}
    assert result.playable_note_count == 4
    assert result.validation_version == SNG_VALIDATION_VERSION


def test_guitar_open_notes_and_instrument_specific_drum_lanes_are_preserved(tmp_path):
    path = tmp_path / "notes.sng"
    path.write_bytes(_chart(("ExpertSingle", [7, 5, 6])))
    assert validate_sng(path, require_five_fret_guitar=True).playable_note_count == 1
    path.write_bytes(_midi("PART GUITAR", [95], enhanced_opens=True))
    assert validate_sng(path, require_five_fret_guitar=True).valid
    path.write_bytes(_midi("PART GUITAR", [95]))
    assert not validate_sng(path, require_five_fret_guitar=True).valid
    path.write_bytes(_midi("PART DRUMS", [65, 89, 101]))
    result = validate_sng(path)
    assert result.valid
    assert dict(result.note_counts) == {"drums:easy": 1, "drums:hard": 1, "drums:expert": 1}
    assert not validate_sng(path, require_five_fret_guitar=True).valid


def _record_notes(db, chart, payload, path, *, overrides=None):
    path.write_bytes(payload)
    validation = validate_sng(path, require_five_fret_guitar=True)
    assert validation.valid, validation.error
    values = {
        "artifact_ref": chart.md5, "state": "VERIFIED",
        "validator_version": SNG_VALIDATION_VERSION,
        "artifact_sha256": hashlib.sha256(payload).hexdigest(),
        "playable_note_count": validation.playable_note_count,
        "validation": asdict(validation),
    }
    values.update(overrides or {})
    db.record_chart_artifact_validation(chart.source, chart.chart_id, **values)


@pytest.mark.parametrize("case", ["old_version", "different_artifact", "missing_counts", "negative_count", "boolean_count", "wrong_total", "missing_hash"])
def test_incomplete_or_stale_note_evidence_keeps_difficulty_unknown(config, tmp_path, case):
    chart = make_chart("Known", "Artist", chart_id=510)
    payload = _chart(("ExpertSingle", [0]))
    db = Database(config.database_path)
    try:
        _record_notes(db, chart, payload, tmp_path / "chart.sng")
        row = dict(db.get_chart_artifact_validation(chart.source, chart.chart_id))
        validation = json.loads(row["validation_json"])
        overrides = {"validation": validation}
        if case == "old_version":
            overrides["validator_version"] = "sng-playability-v1"
        elif case == "different_artifact":
            overrides["artifact_ref"] = "other-artifact"
        elif case == "missing_hash":
            overrides["artifact_sha256"] = ""
        elif case == "missing_counts":
            validation.pop("note_counts")
        elif case == "negative_count":
            validation["note_counts"] = {"guitar:expert": -1}
        elif case == "boolean_count":
            validation["note_counts"] = {"guitar:expert": True}
        else:
            overrides["playable_note_count"] = 2
        _record_notes(db, chart, payload, tmp_path / "chart.sng", overrides=overrides)
        coverage = inspected_difficulty_coverage(db, chart)
        assert coverage == {"evidence": "unknown", "instruments": {}, "guitar_difficulties": [], "expert_only": None, "playable_note_count": None}
    finally:
        db.close()


def test_public_exact_and_expansion_share_inspected_levels_and_prefer_multi_difficulty(client, config, tmp_path):
    _sign_in(client)
    workspace = client.get("/api/journey").get_json()["workspace_id"]
    expert = _chart(("ExpertSingle", [0]))
    multi = _chart(("EasySingle", [0]), ("HardSingle", [1]), ("ExpertSingle", [2]))
    mixed = _chart(("ExpertSingle", [0]), ("EasyDoubleBass", [1]), ("ExpertDoubleBass", [2]))
    db = Database(config.database_path)
    try:
        db.upsert_library_snapshot(workspace, LibrarySnapshot("spotify", [ProviderTrack("spotify", "known", "Known", "Artist")]))
        track = db.get_library_tracks(workspace_id=workspace)[0]
        generation = db.create_discovery_scan(workspace, scan_key="notes", kind="exact", status="complete", metadata={"workflow": "combined_catalog"})
        task = db.upsert_discovery_task(workspace, scan_id=generation, source="test", query_key="notes", status="complete")
        charts = []
        for index, (kind, name, payload) in enumerate([
            ("exact", "Known", expert), ("exact", "Known", multi),
            ("expansion", "More", _chart(("ExpertSingle", [1]))),
            ("expansion", "More", _chart(("EasySingle", [1]), ("HardSingle", [2]), ("ExpertSingle", [3]))),
            ("expansion", "Mixed", mixed),
        ]):
            chart = make_chart(name, "Artist", chart_id=600 + index, md5=hashlib.md5(payload).hexdigest())
            db.upsert_workspace_candidate(workspace, track.track_key if kind == "exact" else None, chart,
                score=99, eligibility="RECOMMENDED", decision="AUTO_ACCEPT", kind=kind,
                task_id=task if kind == "exact" else None, artist_norm="artist" if kind == "expansion" else "",
                metadata={"exact_generation_id": generation, **({"coverage_mode": "all"} if kind == "expansion" else {})})
            _record_notes(db, chart, payload, tmp_path / "chart.sng")
            charts.append(chart)
        db.update_workspace(workspace, status="complete", phase="complete", metadata={"workflow": "public_catalog"})
    finally:
        db.close()
    exact = client.get("/api/workspace").get_json()["tracks"][0]
    assert exact["best_candidate_id"] == charts[1].db_id
    assert exact["candidates"][0]["difficulty_coverage"]["guitar_difficulties"] == ["easy", "hard", "expert"]
    assert exact["candidates"][1]["difficulty_coverage"]["expert_only"] is True
    assert exact["candidates"][1]["selectable"] is True
    songs = {song["title"]: song for song in client.get("/api/expansion/results").get_json()["songs"]}
    assert songs["More"]["candidates"][0]["candidate_id"] == charts[3].db_id
    mixed_coverage = songs["Mixed"]["candidates"][0]["difficulty_coverage"]
    assert mixed_coverage["guitar_difficulties"] == ["expert"]
    assert mixed_coverage["instruments"]["bass"] == ["easy", "expert"]
    assert mixed_coverage["expert_only"] is False
    assert songs["Mixed"]["candidates"][0]["selectable"] is True

    chosen = client.post("/api/selection/bulk", json={"preset": "best_matches"})
    assert chosen.status_code == 200, chosen.get_json()
    assert chosen.get_json()["changed"] == 1
    charter_choice = client.post("/api/selection/bulk", json={"preset": "best_matches", "search": "TestCharter"})
    assert charter_choice.status_code == 200, charter_choice.get_json()
    assert charter_choice.get_json()["changed"] == 1
    assert client.post("/api/expansion/selection/bulk", json={"preset": "one_per_song"}).status_code == 400
    expanded = client.post("/api/expansion/selection/bulk", json={
        "preset": "one_per_song", "confirm_possible": True,
    })
    assert expanded.status_code == 200, expanded.get_json()
    assert expanded.get_json()["summary"]["expansion"] == {"songs": 2, "charts": 2}
    assert expanded.get_json()["songs"] == expanded.get_json()["charts"] == 3
    db = Database(config.database_path)
    try:
        exact_ids = {row["candidate_id"] for row in db.list_workspace_selections(workspace, kind="exact")}
        expansion_ids = {row["candidate_id"] for row in db.list_workspace_selections(workspace, kind="expansion")}
        assert exact_ids == {charts[1].db_id}
        assert expansion_ids == {charts[3].db_id, charts[4].db_id}
    finally:
        db.close()


def test_public_zip_records_verified_note_levels_and_omits_modifier_only_chart(config, tmp_path):
    good_bytes = _chart(("EasySingle", [0]), ("ExpertSingle", [1]))
    bad_bytes = _chart(("ExpertSingle", [5, 6]))
    db = Database(config.database_path)
    try:
        workspace = db.create_workspace(workspace_key="actual-notes")
        db.upsert_library_snapshot(workspace, LibrarySnapshot("spotify", [ProviderTrack("spotify", "good", "Good", "Artist"), ProviderTrack("spotify", "bad", "Bad", "Artist")]))
        files = {}
        charts = {}
        for track in db.get_library_tracks(workspace_id=workspace):
            payload = good_bytes if track.title == "Good" else bad_bytes
            chart = make_chart(track.title, "Artist", chart_id=701 if track.title == "Good" else 702, md5=hashlib.md5(payload).hexdigest())
            db.upsert_workspace_candidate(workspace, track.track_key, chart, score=99, eligibility="RECOMMENDED", decision="AUTO_ACCEPT")
            db.select_workspace_candidate(workspace, track.track_key, chart)
            files[chart.md5] = payload
            charts[track.title] = chart
        archive, summary = build_archive(config, db, workspace, [FakeSource(files=files)], tmp_path / "downloads")
        assert summary["published"] == 1
        assert summary["invalid"] == 1
        with zipfile.ZipFile(archive) as result:
            assert sum(name.endswith(".sng") for name in result.namelist()) == 1
        assert db.get_chart_artifact_validation("test", 702)["state"] == "REJECTED"
        coverage = inspected_difficulty_coverage(db, charts["Good"])
        assert coverage["evidence"] == "inspected_notes"
        assert coverage["guitar_difficulties"] == ["easy", "expert"]
        assert coverage["playable_note_count"] == 2
    finally:
        db.close()


@pytest.mark.parametrize("coverage_mode", [None, "recommended", "all"])
def test_expansion_coverage_mode_is_trusted_and_survives_selection_and_zip(
    client, config, tmp_path, coverage_mode
):
    _sign_in(client)
    workspace = client.get("/api/journey").get_json()["workspace_id"]
    payload = _chart(("ExpertSingle", [0]))
    db = Database(config.database_path)
    try:
        db.upsert_library_snapshot(workspace, LibrarySnapshot("spotify", [ProviderTrack("spotify", "known", "Known", "Artist")]))
        generation = db.create_discovery_scan(workspace, scan_key="coverage", kind="exact", status="complete", metadata={"workflow": "combined_catalog"})
        chart = make_chart("More", "Artist", chart_id=801, md5=hashlib.md5(payload).hexdigest(), instruments=["guitar"])
        metadata = {"exact_generation_id": generation}
        if coverage_mode is not None:
            metadata["coverage_mode"] = coverage_mode
        db.upsert_workspace_candidate(workspace, None, chart, kind="expansion", artist_norm="artist",
            score=60, eligibility="ELIGIBLE_REVIEW", decision="REVIEW", metadata=metadata)
        db.update_workspace(workspace, status="complete", phase="complete", metadata={"workflow": "public_catalog"})
    finally:
        db.close()

    card = client.get("/api/expansion/results").get_json()["songs"][0]["candidates"][0]
    assert card["selectable"] is (coverage_mode == "all")
    assert card["difficulty_coverage"]["evidence"] == "unknown"
    body = {"kind": "expansion", "candidate_id": chart.db_id, "artist_norm": "artist", "selected": True, "coverage_mode": "all"}
    assert client.post("/api/selection", json=body).status_code == 400
    body["confirm_possible"] = True
    selected = client.post("/api/selection", json=body)
    if coverage_mode != "all":
        assert selected.status_code == 400
        db = Database(config.database_path)
        try:
            db.select_workspace_candidate(workspace, None, chart.db_id, kind="expansion", artist_norm="artist",
                force=True, require_workspace_candidate=True,
                metadata={"selection_origin": "explicit", "possible_confirmed": True})
            with pytest.raises(DownloadError, match="No downloadable charts"):
                _validated_selections(db, workspace, config=config)
        finally:
            db.close()
        return
    assert selected.status_code == 200, selected.get_json()
    db = Database(config.database_path)
    try:
        selections = _validated_selections(db, workspace, config=config)
        assert len(selections) == 1
        assert selections[0]["result_tier"] == "POSSIBLE_MATCH"
        archive, summary = build_archive(config, db, workspace, [FakeSource(files={chart.md5: payload})], tmp_path / "all-downloads")
        assert archive.is_file()
        assert summary["published"] == 1
        coverage = inspected_difficulty_coverage(db, chart)
        assert coverage["expert_only"] is True
        assert coverage["guitar_difficulties"] == ["expert"]
    finally:
        db.close()


def test_expansion_bulk_uses_only_each_authenticated_owners_current_songs(app, client, config, monkeypatch):
    anonymous = app.test_client()
    assert anonymous.post("/api/expansion/selection/bulk", json={"confirm_possible": True}).status_code == 401
    _sign_in(client)
    other = app.test_client()
    monkeypatch.setattr("csv_to_clonehero.server.web._fetch_profile", lambda cfg, token: {
        "id": "user-2", "display_name": "Bob", "country": "US", "product": "premium", "href": "",
    })
    _sign_in(other)
    owners = []
    for index, browser in enumerate([client, other], start=1):
        workspace = browser.get("/api/journey").get_json()["workspace_id"]
        artist = f"Artist {index}"
        db = Database(config.database_path)
        try:
            db.upsert_library_snapshot(workspace, LibrarySnapshot("spotify", [ProviderTrack("spotify", f"known-{index}", "Known", artist)]))
            generation = db.create_discovery_scan(workspace, scan_key=f"owner-{index}", kind="exact", status="complete", metadata={"workflow": "combined_catalog"})
            chart = make_chart(f"More {index}", artist, chart_id=900 + index)
            db.upsert_workspace_candidate(workspace, None, chart, kind="expansion", artist_norm=chart.artist_norm,
                score=99, eligibility="RECOMMENDED", decision="AUTO_ACCEPT", metadata={"exact_generation_id": generation})
            db.update_workspace(workspace, status="complete", phase="complete", metadata={"workflow": "public_catalog"})
            owners.append((workspace, chart))
        finally:
            db.close()
    assert owners[0][0] != owners[1][0]
    for index, browser in enumerate([client, other]):
        workspace, own = owners[index]
        foreign_workspace, foreign = owners[1 - index]
        songs = browser.get("/api/expansion/results").get_json()["songs"]
        assert [song["title"] for song in songs] == [own.name]
        forbidden = browser.post("/api/selection", json={
            "kind": "expansion", "candidate_id": foreign.db_id,
            "artist_norm": foreign.artist_norm, "confirm_possible": True,
        })
        assert forbidden.status_code == 400
        selected = browser.post("/api/expansion/selection/bulk", json={
            "preset": "one_per_song", "confirm_possible": True, "workspace_id": foreign_workspace,
        })
        assert selected.status_code == 200, selected.get_json()
        assert selected.get_json()["songs"] == 1
        db = Database(config.database_path)
        try:
            assert {row["candidate_id"] for row in db.list_workspace_selections(workspace, kind="expansion")} == {own.db_id}
            if index:
                assert {row["candidate_id"] for row in db.list_workspace_selections(owners[0][0], kind="expansion")} == {owners[0][1].db_id}
        finally:
            db.close()
