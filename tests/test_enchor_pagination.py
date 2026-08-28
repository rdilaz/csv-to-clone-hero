"""Unit tests for Enchor adapter behavior that do not need the live API."""

import httpx
import pytest

from csv_to_clonehero.normalization import sanitize_query_for_enchor
from csv_to_clonehero.sources.enchor import EnchorSource


def _chart(chart_id: int, name: str = "Song", artist: str = "Artist") -> dict:
    return {"chartId": chart_id, "name": name, "artist": artist}


def test_search_artist_paginates_until_empty(monkeypatch):
    source = EnchorSource(catalog_path="nonexistent_catalog.jsonl")
    calls = []

    def fake_post(path, payload):
        calls.append(payload)
        page = payload["page"]
        if page == 1:
            return {"found": 3, "data": [_chart(1, "One"), _chart(2, "Two")]}
        if page == 2:
            return {"found": 3, "data": [_chart(3, "Three")]}
        return {"found": 3, "data": []}

    monkeypatch.setattr(source, "_post", fake_post)
    results = source.search_artist("Artist")

    assert len(results) == 3
    assert {c.chart_id for c in results} == {1, 2, 3}
    # Stops when accumulated rows reach `found`; no empty trailing page needed.
    assert [c["page"] for c in calls] == [1, 2]


def test_search_artist_stops_when_accumulated_reaches_found(monkeypatch):
    source = EnchorSource(catalog_path="nonexistent_catalog.jsonl")
    calls = []

    def fake_post(path, payload):
        calls.append(payload)
        page = payload["page"]
        # 5 total hits, 2 per page.
        data = [
            _chart((page - 1) * 2 + i + 1, f"Song {page}-{i}")
            for i in range(2)
            if (page - 1) * 2 + i + 1 <= 5
        ]
        return {"found": 5, "data": data}

    monkeypatch.setattr(source, "_post", fake_post)
    results = source.search_artist("Artist")

    assert len(results) == 5
    # Pages 1, 2, 3: page 3 returns the fifth chart and accumulated reaches `found`.
    assert [c["page"] for c in calls] == [1, 2, 3]


def test_search_artist_deduplicates_overlapping_pages(monkeypatch):
    source = EnchorSource(catalog_path="nonexistent_catalog.jsonl")

    def fake_post(path, payload):
        page = payload["page"]
        # Page 2 repeats chart 2 from page 1.
        if page == 1:
            return {"found": 3, "data": [_chart(1), _chart(2)]}
        if page == 2:
            return {"found": 3, "data": [_chart(2), _chart(3)]}
        return {"found": 3, "data": []}

    monkeypatch.setattr(source, "_post", fake_post)
    results = source.search_artist("Artist")

    assert len(results) == 3
    assert {c.chart_id for c in results} == {1, 2, 3}


def test_search_artist_enforces_sanity_cap(monkeypatch):
    source = EnchorSource(catalog_path="nonexistent_catalog.jsonl")
    calls = []

    def fake_post(path, payload):
        calls.append(payload)
        return {"found": 10000, "data": [_chart(payload["page"], f"Song {payload['page']}"), _chart(payload["page"] + 1000)]}

    monkeypatch.setattr(source, "_post", fake_post)
    results = source.search_artist("Artist")

    # 20 page cap x 250 per page, but fake returns 2 per page.
    assert len(calls) == 20
    assert len(results) == 40


def test_song_http_500_falls_back_without_retrying_same_query():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(500, request=request)
        return httpx.Response(
            201,
            request=request,
            json={"found": 1, "data": [_chart(1, "Song", "Artist")]},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    source = EnchorSource(client=client, request_delay=0, retries=3, catalog_path="nonexistent_catalog.jsonl")
    try:
        results = source.search_song("Artist", "Song")
    finally:
        source.close()

    assert len(results) == 1
    assert len(calls) == 2


def test_song_query_sanitizer_removes_spotify_version_noise():
    artist, title = sanitize_query_for_enchor(
        'Artist “Name” (feat. Guest)',
        'Song (feat. Singer [Remix]) (2011 Remaster) - Live Version /',
    )

    assert artist == "Artist Name"
    assert title == "Song"


def test_search_song_sends_sanitized_query(monkeypatch):
    source = EnchorSource(request_delay=0, catalog_path="nonexistent_catalog.jsonl")
    queries = []
    monkeypatch.setattr(source, "_search", lambda query: queries.append(query) or [])
    try:
        source.search_song("Artist (with Guest)", "Song (Deluxe Version)")
    finally:
        source.close()

    assert queries == ["Artist Song"]


def test_known_catalog_chart_can_be_verified_without_search(monkeypatch):
    source = EnchorSource(request_delay=0, catalog_path="nonexistent_catalog.jsonl")
    source.catalog = type(
        "Catalog",
        (),
        {
            "available": True,
            "has_chart": lambda self, chart_id, source_name: int(chart_id) == 7
            and source_name == "enchor",
        },
    )()
    try:
        assert source.catalog_contains_chart(7) is True
        assert source.catalog_contains_chart(8) is False
    finally:
        source.close()


def test_catalog_preflight_uses_one_bounded_authoritative_probe(monkeypatch):
    source = EnchorSource(request_delay=0, catalog_path="nonexistent_catalog.jsonl")
    calls = []
    monkeypatch.setattr(
        source,
        "_post",
        lambda path, payload: calls.append((path, payload)) or {
            "found": 1234,
            "out_of": 1234,
            "page": 1,
            "data": [_chart(1)],
        },
    )
    try:
        result = source.preflight_catalog()
    finally:
        source.close()

    assert result.success is True
    assert result.supported is True
    assert result.authoritative is True
    assert result.out_of == 1234
    assert len(calls) == 1
    assert calls[0][0] == "/search"
    assert calls[0][1]["search"] == ""
    assert calls[0][1]["per_page"] == 1


def test_catalog_preflight_rejects_filtered_or_incomplete_total(monkeypatch):
    source = EnchorSource(request_delay=0, catalog_path="nonexistent_catalog.jsonl")
    monkeypatch.setattr(
        source,
        "_post",
        lambda path, payload: {"found": 12, "out_of": 1234, "page": 1, "data": []},
    )
    try:
        result = source.preflight_catalog()
    finally:
        source.close()

    assert result.success is True
    assert result.supported is True
    assert result.authoritative is False
    assert "not authoritative" in result.error


def test_catalog_preflight_rejects_empty_page_for_positive_total(monkeypatch):
    source = EnchorSource(request_delay=0, catalog_path="nonexistent_catalog.jsonl")
    monkeypatch.setattr(
        source,
        "_post",
        lambda path, payload: {"found": 100, "out_of": 100, "page": 1, "data": []},
    )
    try:
        result = source.preflight_catalog()
    finally:
        source.close()
    assert result.authoritative is False
