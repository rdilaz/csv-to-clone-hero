"""Public readiness reports real availability without blocking a web request."""

from dataclasses import asdict, replace
import json
import threading
import time

import pytest

from csv_to_clonehero.database import Database
from csv_to_clonehero.server.safeguards import (
    get_discovery_availability,
    preflight_public,
    refresh_enchor_preflight,
)
from csv_to_clonehero.server.throttle import global_gate, reset_global_gate_for_tests
from csv_to_clonehero.sources.enchor import EnchorCatalogPreflight
from tests.test_catalog import candidate, write_snapshot


@pytest.fixture(autouse=True)
def clean_gate():
    reset_global_gate_for_tests()
    yield
    reset_global_gate_for_tests()


@pytest.fixture
def db(config):
    database = Database(config.database_path)
    yield database
    database.close()


def successful_probe():
    return EnchorCatalogPreflight(
        checked_at=time.time(), success=True, supported=True, authoritative=True,
        found=321, out_of=321, returned_count=1, page=1,
    )


class FakeEnchor:
    name = "enchor"
    supports_direct_download = True
    exportable = True
    discovery_only = False

    def __init__(self, response, calls):
        self.response = response
        self.calls = calls

    def preflight_catalog(self):
        self.calls.append("probe")
        return self.response

    def close(self):
        self.calls.append("close")


def test_verified_catalog_stays_ready_without_network_even_when_gate_busy(config, db):
    config.paths.catalog = "catalog.jsonl"
    write_snapshot(config.catalog_path, [candidate(1, "Artist", "Song")])

    def no_network(_config):
        raise AssertionError("verified catalog must not construct an online adapter")

    with global_gate().acquire(tag="existing-job"):
        available = get_discovery_availability(config, db)
        refreshed = refresh_enchor_preflight(config, db, no_network)
    assert available == refreshed
    assert available["mode"] == "catalog"
    assert available["status"] == "ready"
    assert available["usable"] and available["catalog_available"]
    assert not available["busy"] and not available["retryable"]
    assert available["eligible_charts"] == 1
    assert db.get_source_preflight("enchor") is None


@pytest.mark.parametrize("catalog_state", ["missing", "incomplete", "corrupt"])
def test_cached_fallback_preserves_mode_and_notice_for_unavailable_catalog(config, db, catalog_state):
    config.paths.catalog = "catalog.jsonl"
    if catalog_state == "incomplete":
        config.catalog_path.write_text(json.dumps({"type": "enchor-catalog", "complete": False}) + "\n")
    elif catalog_state == "corrupt":
        write_snapshot(config.catalog_path, [candidate(1, "Artist", "Song")])
        with config.catalog_path.open("a") as stream:
            stream.write("corrupted body\n")
    db.record_source_preflight(asdict(successful_probe()), ttl_seconds=3600)

    def no_probe(_config):
        raise AssertionError("fresh shared observation must be reused")

    available = get_discovery_availability(config, db)
    refreshed = refresh_enchor_preflight(config, db, no_probe)
    assert refreshed == available
    assert refreshed["mode"] == "live_fallback"
    assert refreshed["status"] == "ready" and refreshed["usable"]
    assert refreshed["catalog_available"] is False
    assert refreshed["catalog_diagnostic"] == "The local chart catalog is unavailable."
    assert str(config.catalog_path) not in json.dumps(refreshed)


def test_new_probe_has_same_readiness_contract_and_is_reused(config, db):
    calls = []
    factory = lambda _config: [FakeEnchor(successful_probe(), calls)]
    first = refresh_enchor_preflight(config, db, factory)
    assert first["mode"] == "live_fallback" and first["catalog_available"] is False
    assert first["status"] == "ready" and first["usable"]
    assert not first["busy"] and not first["retryable"]
    assert first == get_discovery_availability(config, db)
    assert first == refresh_enchor_preflight(config, db, factory)
    assert calls == ["probe", "close"]


def test_busy_probe_returns_before_gate_release_without_caching_failure(config, db):
    expired = replace(successful_probe(), checked_at=1.0)
    db.record_source_preflight(asdict(expired), ttl_seconds=1)
    previous = dict(db.get_source_preflight("enchor"))
    calls = []
    results = []

    def refresh():
        connection = Database(config.database_path)
        try:
            results.append(refresh_enchor_preflight(
                config, connection, lambda _config: [FakeEnchor(successful_probe(), calls)]
            ))
        finally:
            connection.close()

    with global_gate().acquire(tag="existing-long-job"):
        thread = threading.Thread(target=refresh)
        thread.start()
        thread.join(timeout=1)
        returned_while_busy = not thread.is_alive()
    thread.join(timeout=2)
    assert returned_while_busy, "public probe waited for the entire job"
    assert results[0]["status"] == "busy"
    assert results[0]["busy"] and results[0]["retryable"]
    assert not results[0]["usable"]
    assert results[0]["retry_after_seconds"] == 1
    assert results[0]["mode"] == "live_fallback"
    assert calls == []
    assert dict(db.get_source_preflight("enchor")) == previous
    recovered = refresh_enchor_preflight(config, db, lambda _: [FakeEnchor(successful_probe(), calls)])
    assert recovered["usable"] and recovered["status"] == "ready"
    assert calls == ["probe", "close"]


@pytest.mark.parametrize("rate_limited", [False, True])
def test_failed_probe_keeps_private_operator_error_out_of_public_status(config, db, rate_limited):
    private_error = "unable to read C:\\private-host\\catalog-secret.jsonl"
    failure = EnchorCatalogPreflight(
        checked_at=time.time(), error=private_error,
        metadata={"rate_limited": rate_limited, "http_status": 429 if rate_limited else 500,
                  "private_detail": private_error},
    )
    calls = []
    result = refresh_enchor_preflight(config, db, lambda _: [FakeEnchor(failure, calls)])
    assert result["mode"] == "live_fallback"
    assert result["status"] == "unavailable" and result["retryable"]
    assert not result["usable"] and not result["busy"]
    assert "Your music choices are kept" in result["message"]
    assert ("limiting requests" in result["message"]) is rate_limited
    assert private_error not in json.dumps(result)
    assert db.get_source_preflight("enchor")["error"] == private_error
    assert calls == ["probe", "close"]


def test_broad_probe_without_authority_stays_unusable(config, db):
    probe = replace(successful_probe(), authoritative=False)
    calls = []
    result = refresh_enchor_preflight(config, db, lambda _: [FakeEnchor(probe, calls)])
    assert result["success"] and result["supported"]
    assert not result["authoritative"] and not result["usable"]
    assert result["status"] == "unavailable"
    observation = db.get_source_preflight("enchor")
    assert observation["expires_at"] - observation["checked_at"] <= 60
    assert 0 < result["retry_after_seconds"] <= 60
    assert result == refresh_enchor_preflight(config, db, lambda _: pytest.fail("negative cache must prevent a repeat probe"))


def test_old_hour_cached_unusable_probe_expires_after_short_failure_window(config, db):
    probe = replace(successful_probe(), checked_at=100.0, authoritative=False)
    db.record_source_preflight(asdict(probe), ttl_seconds=3600)
    observation = dict(db.get_source_preflight("enchor"))
    early = preflight_public(observation, now=100.0)
    assert early["fresh"] and not early["usable"]
    assert early["expires_at"] == 160.0 and early["retry_after_seconds"] == 60
    expired = preflight_public(observation, now=160.0)
    assert not expired["fresh"] and expired["status"] == "stale"
    assert expired["retry_after_seconds"] == 0
    # Interpret the shorter policy; do not change the historical observation.
    assert dict(db.get_source_preflight("enchor")) == observation
    assert observation["expires_at"] == 3700.0
    calls = []
    recovered = refresh_enchor_preflight(config, db, lambda _: [FakeEnchor(successful_probe(), calls)])
    assert recovered["usable"] and calls == ["probe", "close"]


def test_verified_probe_keeps_long_success_cache_and_no_retry_delay(config, db):
    probe = replace(successful_probe(), checked_at=100.0)
    db.record_source_preflight(asdict(probe), ttl_seconds=3600)
    result = preflight_public(db.get_source_preflight("enchor"), now=160.0)
    assert result["fresh"] and result["usable"]
    assert result["expires_at"] == 3700.0
    assert not result["retryable"] and result["retry_after_seconds"] == 0


def test_missing_probe_is_check_required_and_retryable(config, db):
    result = get_discovery_availability(config, db)
    assert result["status"] == "check_required"
    assert result["mode"] == "live_fallback"
    assert result["retryable"] and not result["busy"] and not result["usable"]
