"""Unit tests for Enchor adapter behavior that do not need the live API."""

import httpx
import pytest

from csv_to_clonehero.sources.enchor import EnchorSource


def _chart(chart_id: int, name: str = "Song", artist: str = "Artist") -> dict:
    return {"chartId": chart_id, "name": name, "artist": artist}


def test_search_artist_paginates_until_empty(monkeypatch):
    source = EnchorSource()
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
    source = EnchorSource()
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
    source = EnchorSource()

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
    source = EnchorSource()
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
    source = EnchorSource(client=client, request_delay=0, retries=3)
    try:
        results = source.search_song("Artist", "Song")
    finally:
        source.close()

    assert len(results) == 1
    assert len(calls) == 2
