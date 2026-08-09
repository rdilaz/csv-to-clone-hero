from csv_to_clonehero.csv_import import artist_track_counts, import_playlist_dir

from .conftest import FIXTURES


def test_import_reads_all_files():
    result = import_playlist_dir(FIXTURES)
    assert result.total_rows == 10
    assert set(result.file_row_counts) == {"playlist_rock.csv", "playlist_mixed.csv"}


def test_deduplication_across_files():
    result = import_playlist_dir(FIXTURES)
    # 10 rows; Everlong appears 3 times (2x in rock, 1x in mixed) -> 8 unique.
    assert len(result.tracks) == 8
    everlong = next(t for t in result.tracks if t.title == "Everlong")
    assert set(everlong.playlists) == {"playlist_rock", "Chill Mix"}


def test_duplicate_rows_merge_metadata():
    result = import_playlist_dir(FIXTURES)
    everlong = next(t for t in result.tracks if t.title == "Everlong")
    assert everlong.spotify_id == "3GJ6pKBu3qPoOp1WvL8G1H"
    assert everlong.album == "The Colour and the Shape"


def test_dedup_without_spotify_id():
    result = import_playlist_dir(FIXTURES)
    obscure = next(t for t in result.tracks if t.title == "Song Without Id")
    assert obscure.track_key == "obscure artist|song without id"


def test_header_aliases_recognized():
    # playlist_mixed.csv uses Song/Artist/Spotify URI/Playlist headers.
    result = import_playlist_dir(FIXTURES)
    humble = next(t for t in result.tracks if t.title == "Humble")
    assert humble.spotify_id == "7KXjTSCq5nL1LoYtL7XAwS"
    assert humble.primary_artist == "Kendrick Lamar"


def test_featured_artists_extracted():
    result = import_playlist_dir(FIXTURES)
    feel_good = next(t for t in result.tracks if t.title.startswith("Feel Good"))
    assert feel_good.primary_artist == "Gorillaz"
    assert feel_good.featured_artists == ["De La Soul"]


def test_artist_track_counts():
    result = import_playlist_dir(FIXTURES)
    primary, featured_only = artist_track_counts(result.tracks)
    assert primary["foo fighters"] == 1
    assert primary["the rolling stones"] == 2
    assert featured_only["de la soul"] == 1


def test_title_with_comma_parsed_correctly():
    result = import_playlist_dir(FIXTURES)
    paint = next(t for t in result.tracks if t.primary_artist == "The Rolling Stones"
                 and "Paint" in t.title)
    assert paint.title == "Paint It, Black - 2009 Remaster"
    assert paint.title_norm == "paint it black"
