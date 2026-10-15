"""Portable companion defaults and distribution launch boundaries (offline)."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from csv_to_clonehero import companion


def load_bootstrap():
    path = Path(__file__).resolve().parents[1] / "distribution" / "companion" / "bootstrap.py"
    spec = importlib.util.spec_from_file_location("riff_distribution_bootstrap", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_defaults_are_portable_local_empty_and_use_waitress(tmp_path, monkeypatch):
    import waitress
    calls = []
    monkeypatch.setattr(waitress, "serve", lambda app, **kwargs: calls.append((app, kwargs)))
    monkeypatch.setattr(sys, "argv", ["companion", "--data-dir", str(tmp_path / "local")])
    companion.main()
    app, options = calls[0]
    assert options["host"] == "127.0.0.1" and options["port"] == 8947
    body = app.test_client().get("/api/companion/status").get_json()
    assert body["catalog_count"] == 0 and body["catalog"]["available"] is False
    assert body["cached_artifacts"] == 0 and body["library"]["roots"] == []
    assert body["network_downloads"] is False
    assert Path(body["sandbox_root"]) == tmp_path / "local" / "sandbox"
    assert Path(body["sandbox_root"]).is_dir()


@pytest.mark.parametrize("port", ["0", "-1", "65536"])
def test_cli_invalid_ports_do_not_create_files(tmp_path, monkeypatch, port):
    monkeypatch.setattr(sys, "argv", ["companion", "--data-dir", str(tmp_path / "data"), "--port", port])
    with pytest.raises(SystemExit):
        companion.main()
    assert not (tmp_path / "data").exists()


def test_cli_rejects_library_and_sandbox_overlap_before_creating_files(tmp_path, monkeypatch):
    library = tmp_path / "Songs"
    library.mkdir()
    sandbox = library / "must-not-create"
    monkeypatch.setattr(sys, "argv", ["companion", "--library-root", str(library), "--sandbox-root", str(sandbox)])
    with pytest.raises(SystemExit):
        companion.main()
    assert list(library.iterdir()) == []


@pytest.mark.parametrize("input_flag,is_file", [("--artifact-root", False), ("--catalog", True)])
def test_cli_rejects_sandbox_inside_read_only_inputs(tmp_path, monkeypatch, input_flag, is_file):
    protected = tmp_path / "input"
    protected.mkdir()
    value = protected / "catalog.jsonl" if is_file else protected
    if is_file:
        value.write_text("existing metadata", encoding="utf-8")
    sandbox = protected if is_file else protected / "must-not-create"
    monkeypatch.setattr(sys, "argv", ["companion", input_flag, str(value), "--sandbox-root", str(sandbox)])
    with pytest.raises(SystemExit):
        companion.main()
    assert not (protected / "must-not-create").exists()


def test_companion_csp_confines_browser_media_and_requests(tmp_path):
    client = companion.create_app(catalog_path=tmp_path / "missing.jsonl", sandbox_root=tmp_path).test_client()
    response = client.get("/")
    assert response.status_code == 200
    csp = response.headers["Content-Security-Policy"]
    assert "connect-src 'self'" in csp and "media-src 'self' blob:" in csp
    assert "frame-ancestors 'none'" in csp


@pytest.mark.parametrize("value", [[], {"unknown": "value"}, {"port": True}, {"port": 70000}, {"library_roots": "not-a-list"}, {"catalog": 42}])
def test_launcher_settings_reject_unknown_or_invalid_values(tmp_path, value):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError):
        load_bootstrap().parse_settings(settings)


def test_launcher_empty_settings_and_explicit_paths(tmp_path):
    module = load_bootstrap()
    assert module.parse_settings(tmp_path / "absent.json") == {}
    settings = tmp_path / "settings.json"
    expected = {"catalog": "D:/catalog/enchor.jsonl", "library_roots": ["D:/Songs"], "artifact_roots": [], "port": 8948}
    settings.write_text(json.dumps(expected), encoding="utf-8")
    assert module.parse_settings(settings) == expected


def test_configure_never_writes_settings_into_read_only_library(tmp_path, monkeypatch):
    library = tmp_path / "Songs"
    library.mkdir()
    values = iter(["", "", str(library)])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(values))
    with pytest.raises(ValueError):
        load_bootstrap().configure(library / "private-settings")
    assert list(library.iterdir()) == []


@pytest.mark.parametrize("kind", ["artifact", "catalog"])
def test_configure_preserves_other_read_only_input_folders(tmp_path, monkeypatch, kind):
    protected = tmp_path / "input"
    protected.mkdir()
    catalog = protected / "catalog.jsonl"
    if kind == "catalog":
        catalog.write_text("existing metadata", encoding="utf-8")
    values = iter([str(catalog) if kind == "catalog" else "", str(protected) if kind == "artifact" else "", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(values))
    with pytest.raises(ValueError):
        load_bootstrap().configure(protected / "must-not-create")
    assert not (protected / "must-not-create").exists()
