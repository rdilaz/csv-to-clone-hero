from __future__ import annotations

import pytest
from pydantic import ValidationError

from csv_to_clonehero.config import load_config


@pytest.fixture
def settings_path(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("export:\n  minimum_free_bytes: 128\n", encoding="utf-8")
    return path


@pytest.mark.parametrize("value, expected", [("0", 0), ("4096", 4096), (" 4096 ", 4096)])
def test_disk_reserve_environment_overrides_yaml_without_rewriting_it(settings_path, monkeypatch, value, expected):
    original = settings_path.read_bytes()
    monkeypatch.setenv("EXPORT_MINIMUM_FREE_BYTES", value)

    config = load_config(settings_path)

    assert config.export.minimum_free_bytes == expected
    assert settings_path.read_bytes() == original


@pytest.mark.parametrize("value", [None, "", "  "])
def test_unset_or_blank_disk_reserve_preserves_yaml(settings_path, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("EXPORT_MINIMUM_FREE_BYTES", raising=False)
    else:
        monkeypatch.setenv("EXPORT_MINIMUM_FREE_BYTES", value)

    assert load_config(settings_path).export.minimum_free_bytes == 128


@pytest.mark.parametrize("value", ["-1", "1.5", "true", "false", "not-a-number"])
def test_invalid_environment_disk_reserve_fails_closed(settings_path, monkeypatch, value):
    monkeypatch.setenv("EXPORT_MINIMUM_FREE_BYTES", value)

    with pytest.raises(ValueError, match="EXPORT_MINIMUM_FREE_BYTES must be a non-negative integer"):
        load_config(settings_path)


@pytest.mark.parametrize("value", ["-1", "true", "1.5"])
def test_invalid_yaml_disk_reserve_is_rejected(settings_path, monkeypatch, value):
    monkeypatch.delenv("EXPORT_MINIMUM_FREE_BYTES", raising=False)
    settings_path.write_text(f"export:\n  minimum_free_bytes: {value}\n", encoding="utf-8")

    with pytest.raises(ValidationError, match="minimum_free_bytes"):
        load_config(settings_path)


def test_public_wsgi_configuration_receives_environment_disk_reserve(settings_path, monkeypatch):
    from csv_to_clonehero.server.web import load_app_config

    monkeypatch.chdir(settings_path.parent)
    monkeypatch.setenv("APP_BASE_URL", "https://riff.ryo-nd.com")
    monkeypatch.setenv("SECRET_KEY", "test-session-key")
    monkeypatch.setenv("EXPORT_MINIMUM_FREE_BYTES", "4096")

    assert load_app_config().export.minimum_free_bytes == 4096
