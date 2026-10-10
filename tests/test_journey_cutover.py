"""Slice 2.5 primary-surface, startup, and asset regressions."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

import csv_to_clonehero.webapp as webapp_module
from csv_to_clonehero.database import Database
from csv_to_clonehero.providers.base import LibrarySnapshot, ProviderTrack
from csv_to_clonehero.webapp import create_app


PRODUCTION_ROOT = Path(__file__).parents[1] / "src" / "csv_to_clonehero"
PRODUCTION_ASSETS = (
    PRODUCTION_ROOT / "templates" / "journey.html",
    PRODUCTION_ROOT / "static" / "journey.css",
    PRODUCTION_ROOT / "static" / "journey.js",
)

# The current local API intentionally retains compatibility aliases. This is a
# bound on the route table, not a timing assertion or a precise implementation
# detail.
MAX_SANE_LOCAL_ROUTES = 180
MAX_SANE_LOCAL_ROUTE_METHODS = 200


@pytest.fixture
def cutover_app(config):
    config.input_path.mkdir(parents=True, exist_ok=True)
    app = create_app(config, source_factory=lambda _config: [])
    app.config["TESTING"] = True
    return app


def _explicit_route_methods(app):
    return Counter(
        (rule.rule, method)
        for rule in app.url_map.iter_rules()
        for method in set(rule.methods) - {"HEAD", "OPTIONS"}
    )


def test_create_app_startup_and_route_inventory(cutover_app):
    app = cutover_app
    rules = list(app.url_map.iter_rules())
    combinations = _explicit_route_methods(app)

    assert rules
    assert len(rules) <= MAX_SANE_LOCAL_ROUTES
    assert sum(combinations.values()) <= MAX_SANE_LOCAL_ROUTE_METHODS
    assert not [key for key, count in combinations.items() if count > 1]

    expected = {
        ("/", "GET"),
        ("/journey", "GET"),
        ("/advanced", "GET"),
        ("/api/journey", "GET"),
        ("/healthz", "GET"),
    }
    assert expected <= set(combinations)
    assert all(rule.rule.startswith("/") for rule in rules)
    assert all("None" not in rule.rule for rule in rules)

    aliases = {}
    for rule in rules:
        aliases.setdefault(rule.endpoint, set()).add(rule.rule)
    assert {"/api/discovery/start", "/api/spotify/scan"} <= aliases["spotify_scan_start"]
    assert {"/api/journey"} <= aliases["journey_read_model"]


def test_create_app_is_not_recursively_constructed(config, monkeypatch):
    config.input_path.mkdir(parents=True, exist_ok=True)
    original = webapp_module.create_app
    calls = []

    def counted(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(webapp_module, "create_app", counted)
    app = webapp_module.create_app(config, source_factory=lambda _config: [])

    assert app is not None
    assert len(calls) == 1


def test_route_registration_is_not_cumulative_across_app_instances(config):
    config.input_path.mkdir(parents=True, exist_ok=True)
    first = create_app(config, source_factory=lambda _config: [])
    second = create_app(config, source_factory=lambda _config: [])

    first_rules = list(first.url_map.iter_rules())
    second_rules = list(second.url_map.iter_rules())
    assert len(first_rules) == len(second_rules)
    assert sum(_explicit_route_methods(first).values()) == sum(_explicit_route_methods(second).values())


def test_root_is_the_only_normal_journey_page(cutover_app):
    client = cutover_app.test_client()
    root = client.get("/")
    html = root.get_data(as_text=True)

    assert root.status_code == 200
    assert 'data-app-surface="journey"' in html
    assert 'data-test="journey-loading"' in html
    assert "Restoring your library..." in html
    assert "journey.css?v=2.6.0-ux" in html
    assert "journey.js?v=2.6.0-ux" in html
    assert "/advanced" not in html
    assert "WHAT'S HAPPENING" not in html
    assert "Preview chart matches" not in html
    assert "Review your matches" not in html
    assert "globalOperation" not in html
    assert "dry-run" not in html
    assert "music-ready" not in html
    assert "matching-running" not in html

    journey = client.get("/journey")
    assert journey.status_code == 302
    assert journey.headers["Location"] == "/"
    assert client.get("/journey", follow_redirects=True).request.path == "/"

    advanced = client.get("/advanced?workspace_id=ignored&state=legacy")
    assert advanced.status_code == 302
    assert advanced.headers["Location"] == "/"
    assert "Review and export charts" not in advanced.get_data(as_text=True)


def test_journey_api_and_health_identity_are_read_only(cutover_app):
    client = cutover_app.test_client()
    assert cutover_app.extensions["spotify_job"].running is False

    journey = client.get("/api/journey")
    assert journey.status_code == 200
    payload = journey.get_json()
    assert payload["schema_version"] == 1
    assert payload["app_surface"] == "journey"
    assert payload["matching"]["state"] == "music_ready"
    assert cutover_app.extensions["spotify_job"].running is False

    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.get_json() == {
        "ok": True,
        "app_surface": "journey",
        "application_surface": "journey",
        "schema_version": 1,
    }


def test_journey_shell_and_assets_cannot_be_immutable(cutover_app):
    client = cutover_app.test_client()
    for path in ("/", "/api/journey", "/static/journey.css?v=2.6.0-ux", "/static/journey.js?v=2.6.0-ux"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert "immutable" not in response.headers["Cache-Control"]


def test_production_journey_assets_contain_no_prototype_fixture_values():
    fixture_only_values = (
        "1,842",
        "37%",
        "James Tillman",
        "And Then",
        "8m 42s",
        "3,470",
        "2,168",
        "842 / 3,160",
        "731 downloaded",
        "111 already available",
        "PROTOTYPE_2_FIXTURES",
    )
    for path in PRODUCTION_ASSETS:
        text = path.read_text(encoding="utf-8")
        assert not any(value in text for value in fixture_only_values), path

    script = (PRODUCTION_ROOT / "static" / "journey.js").read_text(encoding="utf-8")
    assert "fixtures" not in script.casefold()
    assert "setInterval" not in script
    assert "/api/spotify/status" not in script
    assert "/api/discovery/status" not in script
    assert 'fetch(apiUrl("/api/journey")' in script
    assert "if (revision !== model.requestRevision) return;" in script
    assert "schedulePolling(snapshot);" in script
    assert "data-test=\"journey-error\"" in script
    assert ">Retry<" in script


def test_legacy_inline_pages_are_removed_from_local_delivery():
    source = (PRODUCTION_ROOT / "webapp.py").read_text(encoding="utf-8")
    assert "_UNIFIED_PAGE" not in source
    assert "_ADVANCED_PAGE" not in source
    assert "source-neutral-mode" not in source


def test_launcher_starts_real_app_without_normal_reload():
    launcher = (Path(__file__).parents[1] / "WEB_INTERFACE.bat").read_text(encoding="utf-8")
    assert "csv_to_clonehero web --port 8765 %*" in launcher
    assert "static HTTP server" not in launcher
    assert "--reload %*" not in launcher
    assert "Serving real Flask journey app at: http://127.0.0.1:8765/" in launcher
    assert "Get-NetTCPConnection -LocalPort 8765" in launcher


def test_journey_error_and_loading_contract_never_fall_back_to_old_screen():
    html = (PRODUCTION_ROOT / "templates" / "journey.html").read_text(encoding="utf-8")
    script = (PRODUCTION_ROOT / "static" / "journey.js").read_text(encoding="utf-8")

    assert 'data-test="journey-loading"' in html
    assert "journeyAssetError" in html
    assert 'journeyAssetError(\'script\')' in html
    assert 'data-test=\"journey-error\"' in script
    assert "Retry" in script
    assert "prototype-2" not in html.casefold()
    assert "WHAT'S HAPPENING" not in script
    assert "fixture" not in script.casefold()


def test_matching_elapsed_contract_uses_real_server_start():
    script = (PRODUCTION_ROOT / "static" / "journey.js").read_text(encoding="utf-8")
    assert "started_at" in script
    assert "Date.now() / 1000 - startedValue" in script
    assert "matching-elapsed" in script
    assert "window.setTimeout(updateElapsedDisplay, 1000)" in script


def test_matching_fingerprint_tracks_only_current_provider_imports(config):
    db = Database(config.database_path)
    try:
        workspace = db.create_workspace(workspace_key="fingerprint", name="Fingerprint")
        db.upsert_library_snapshot(workspace, LibrarySnapshot("soundcloud", [
            ProviderTrack(provider="soundcloud", provider_id="a", artist="Artist", title="A"),
        ]))
        first = db.get_library_matching_fingerprint(workspace)
        db.upsert_library_snapshot(workspace, LibrarySnapshot("soundcloud", [
            ProviderTrack(provider="soundcloud", provider_id="b", artist="Artist", title="B"),
        ]))
        middle = db.get_library_matching_fingerprint(workspace)
        db.upsert_library_snapshot(workspace, LibrarySnapshot("soundcloud", [
            ProviderTrack(provider="soundcloud", provider_id="c", artist="Artist", title="A"),
        ]))
        last = db.get_library_matching_fingerprint(workspace)
    finally:
        db.close()

    assert first != middle
    assert last == first


def test_discovery_scan_completion_preserves_real_start_time(config):
    db = Database(config.database_path)
    try:
        workspace = db.create_workspace(workspace_key="scan-time", name="Scan time")
        scan_id = db.create_discovery_scan(workspace, scan_key="scan", kind="exact", status="running")
        started = db._conn.execute("SELECT started_at FROM discovery_scans WHERE id=?", (scan_id,)).fetchone()["started_at"]
        db.create_discovery_scan(workspace, scan_key="scan", kind="exact", status="complete")
        finished = db._conn.execute("SELECT started_at, finished_at FROM discovery_scans WHERE id=?", (scan_id,)).fetchone()
    finally:
        db.close()

    assert finished["started_at"] == started
    assert finished["finished_at"] >= started
