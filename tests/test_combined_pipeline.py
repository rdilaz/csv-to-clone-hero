from csv_to_clonehero.combined_pipeline import discover_combined, rank_expansion_artists
from csv_to_clonehero.database import Database
from csv_to_clonehero.providers.base import LibrarySnapshot, ProviderTrack


class FakeCatalog:
    available = True
    diagnostic = ""

    def __init__(self, chart):
        self.chart = chart

    def candidates(self, artist, title):
        return (self.chart,) if (artist, title) == ("Artist", "Song") else ()

    def artist_songs(self, artist):
        return (("Song", (self.chart,)),) if artist == "artist" else ()


def test_combined_discovery_deduplicates_provenance_and_scores(config):
    from tests.conftest import make_chart

    chart = make_chart("Song", "Artist", chart_id=12)
    snapshots = [
        LibrarySnapshot("spotify", [ProviderTrack("spotify", "sp-1", "Song", "Artist", playlist_names=["Mix"])]),
        LibrarySnapshot("soundcloud", [ProviderTrack("soundcloud", "sc-1", "Song", "Artist", liked=True)]),
    ]
    db = Database(config.database_path)
    try:
        result = discover_combined(config, db, snapshots, workspace_key="combined:test", catalog=FakeCatalog(chart))
        assert result.tracks == 1
        assert result.exact_hits == 1
        assert result.candidates == 1
        track = db.get_spotify_tracks(True)[0]
        providers = track.origin_metadata["provenance"]
        assert {entry["provider"] for entry in providers} == {"spotify", "soundcloud"}
    finally:
        db.close()


def test_expansion_artist_ranking_only_uses_imported_artists():
    class Catalog:
        available = True

        def artist_songs(self, artist):
            return (("Song", (object(),)),) * (3 if artist == "artist" else 1)

    ranked = rank_expansion_artists(Catalog(), [type("Track", (), {"primary_artist_norm": "artist"})()])
    assert ranked[0]["artist"] == "artist"
    assert ranked[0]["catalog_songs"] == 3
