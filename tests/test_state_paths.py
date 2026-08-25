"""Path decoupling: state DB default/migration and per-run output override."""

import pytest

from csv_to_clonehero.config import migrate_state_db


def test_database_default_is_outside_output_dir(config):
    assert config.paths.database == "state/csv_to_clonehero.db"
    assert config.database_path.parent.parent == config.base_dir
    assert "output_library" not in str(config.database_path)


def test_migration_moves_legacy_output_library_db(config):
    legacy = config.base_dir / "output_library" / "state" / "library.db"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"sqlite")
    note = migrate_state_db(config)
    assert "Moved state database" in note
    assert not legacy.exists()
    assert config.database_path.read_bytes() == b"sqlite"


def test_migration_moves_legacy_state_dir_db(config):
    legacy = config.base_dir / "state" / "library.db"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"sqlite")
    note = migrate_state_db(config)
    assert "Moved state database" in note
    assert config.database_path.read_bytes() == b"sqlite"


def test_migration_noop_when_nothing_to_move(config):
    assert migrate_state_db(config) == ""
    config.database_path.parent.mkdir(parents=True)
    config.database_path.write_bytes(b"sqlite")
    assert migrate_state_db(config) == ""


def test_migration_refuses_when_both_exist(config):
    legacy = config.base_dir / "state" / "library.db"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"legacy")
    config.database_path.parent.mkdir(parents=True, exist_ok=True)
    config.database_path.write_bytes(b"current")
    with pytest.raises(RuntimeError, match="legacy"):
        migrate_state_db(config)


def test_migration_skipped_for_custom_database_path(config):
    config.paths.database = "custom/my.db"
    legacy = config.base_dir / "state" / "library.db"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"sqlite")
    assert migrate_state_db(config) == ""
    assert legacy.exists()  # untouched


def test_with_output_dir_pins_state_and_reports(config):
    overridden = config.with_output_dir("D:/CH Songs")
    assert overridden.library_root == overridden.resolve("D:/CH Songs")
    assert overridden.database_path == config.database_path
    assert overridden.reports_path == config.reports_path
    assert overridden.paths.output_dir == "D:/CH Songs"
    # Original config unchanged.
    assert config.paths.output_dir == "output_library"
