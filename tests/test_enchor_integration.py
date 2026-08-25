"""Live Enchor API integration tests.

Skipped by default. Run explicitly with:  pytest -m integration
"""

import pytest

from csv_to_clonehero.sources.enchor import EnchorSource

pytestmark = pytest.mark.integration


@pytest.fixture
def source():
    src = EnchorSource(request_delay=0.3, retries=2, timeout=20)
    yield src
    src.close()


def test_search_song(source):
    results = source.search_song("The Rolling Stones", "Paint It Black")
    assert results, "expected at least one candidate"
    candidate = results[0]
    assert candidate.md5
    assert candidate.chart_id > 0
    assert candidate.artist


def test_search_artist(source):
    results = source.search_artist("Foo Fighters")
    assert results
    assert any("foo fighters" in c.artist_norm for c in results)


def test_head_size(source):
    results = source.search_song("Foo Fighters", "Everlong")
    assert results
    size = source.head_size(results[0])
    assert size is None or size > 1000


def test_download_info(source):
    results = source.search_song("Foo Fighters", "Everlong")
    info = source.get_download_info(results[0])
    assert info.supports_direct_download
    assert info.url.startswith("https://files.enchor.us/")


def test_search_artist_pagination_returns_many_unique_charts(source):
    """A prolific cataloged artist should return >250 unique chart ids."""
    results = source.search_artist("Metallica")
    chart_ids = {c.chart_id for c in results}
    assert len(chart_ids) > 250, f"expected pagination to exceed 250 charts, got {len(chart_ids)}"
    assert len(chart_ids) == len(results), "pagination returned duplicate chart ids"
