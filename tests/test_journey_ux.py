"""Local atomic choice recovery and truthful host capabilities."""

import os

from csv_to_clonehero.database import Database
from .test_webapp import client, seed_local_workspace


def _setup(client, config):
    workspace = client.get("/api/journey").get_json()["workspace_id"]
    seed_local_workspace(config, workspace)
    db = Database(config.database_path)
    rows = db.get_workspace_candidates(workspace, kind="exact", include_rejected=True)
    custom = next(row for row in rows if row["chart_id"] == 302)
    db.select_workspace_candidate(workspace, custom["track_id"], custom["candidate_id"],
        kind="exact", eligibility="ELIGIBLE_REVIEW", metadata={"custom_choice": True})
    db.close()
    return workspace, custom["candidate_id"]


def _tray(client, workspace):
    return client.get(f"/api/discovery/selections?workspace_id={workspace}").get_json()


def test_local_cohort_replaces_atomically_and_undo_restores_custom_version(client, config):
    workspace, custom = _setup(client, config)
    before = _tray(client, workspace)
    response = client.post("/api/discovery/select-cohort", json={
        "workspace_id": workspace, "scope": "strong", "expected_revision": before["selection_revision"],
    })
    assert response.status_code == 200, response.get_json()
    after = response.get_json()
    assert after["undo"]["available"] is True
    assert after["items"][0]["candidate_id"] != custom
    restored = client.post("/api/discovery/selection/undo", json={
        "workspace_id": workspace, "expected_revision": after["selection_revision"],
        "operation_id": after["undo"]["operation_id"],
    })
    assert restored.status_code == 200, restored.get_json()
    assert restored.get_json()["items"][0]["candidate_id"] == custom
    assert restored.get_json()["undo"]["available"] is False
    db = Database(config.database_path)
    assert '"custom_choice": true' in db.list_workspace_selections(workspace)[0]["metadata_json"]
    db.close()


def test_local_cohort_failure_preserves_original_choices(client, config, monkeypatch):
    workspace, custom = _setup(client, config)
    before = _tray(client, workspace)

    def fail(*_args, **_kwargs):
        raise ValueError("simulated selection failure after clear")

    monkeypatch.setattr(Database, "select_workspace_candidate", fail)
    response = client.post("/api/discovery/select-cohort", json={
        "workspace_id": workspace, "scope": "strong", "expected_revision": before["selection_revision"],
    })
    assert response.status_code == 409
    after = _tray(client, workspace)
    assert after["items"][0]["candidate_id"] == custom
    assert after["selection_revision"] == before["selection_revision"]


def test_local_undo_rejects_newer_choice_and_eligible_scope_requires_confirmation(client, config):
    workspace, _custom = _setup(client, config)
    before = _tray(client, workspace)
    assert client.post("/api/discovery/select-cohort", json={
        "workspace_id": workspace, "scope": "eligible", "expected_revision": before["selection_revision"],
    }).status_code == 409
    first = client.post("/api/discovery/selection/change", json={
        "workspace_id": workspace, "action": "clear", "expected_revision": before["selection_revision"],
    }).get_json()
    second = client.post("/api/discovery/select-cohort", json={
        "workspace_id": workspace, "scope": "strong", "expected_revision": first["selection_revision"],
    }).get_json()
    stale = client.post("/api/discovery/selection/undo", json={
        "workspace_id": workspace, "expected_revision": first["selection_revision"],
        "operation_id": first["undo"]["operation_id"],
    })
    assert stale.status_code == 409
    assert _tray(client, workspace)["selection_revision"] == second["selection_revision"]


def test_local_capabilities_name_host_and_actual_picker_constraints(client):
    capabilities = client.get("/api/journey").get_json()["capabilities"]
    assert capabilities["surface"] == "local"
    assert capabilities["delivery"] == "host_folder"
    assert capabilities["native_folder_picker"] == (os.name == "nt")
    assert capabilities["manual_destination"] is True
    assert capabilities["configured_destination"]
    assert capabilities["game_scan_required"] is True
