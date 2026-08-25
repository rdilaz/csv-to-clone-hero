import pytest

from csv_to_clonehero.csv_import import (
    artist_track_counts,
    import_itunes_xml,
    import_playlist_dir,
)

from .conftest import FIXTURES

ITUNES_FIXTURES = FIXTURES / "itunes"


def test_import_reads_all_files():
    result = import_playlist_dir(FIXTURES)
    assert result.total_rows == 10
    assert set(result.file_row_counts) == {"playlist_rock.csv", "playlist_mixed.csv"}


def test_import_skips_disabled_files(tmp_path):
    (tmp_path / "a.csv").write_text("Song,Artist\nA,B\n", encoding="utf-8")
    (tmp_path / "b.csv").write_text("Song,Artist\nC,D\n", encoding="utf-8")
    result = import_playlist_dir(tmp_path, skip={"a.csv"})
    assert result.total_rows == 1
    assert set(result.file_row_counts) == {"b.csv"}
    assert result.tracks[0].title == "C"


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


# ----------------------------------------------------------- iTunes import
def test_itunes_txt_tab_delimited_import():
    from csv_to_clonehero.csv_import import import_csv_file

    tracks = import_csv_file(ITUNES_FIXTURES / "itunes_playlist.txt")
    by_title = {t.title: t for t in tracks}
    everlong = by_title["Everlong"]
    assert everlong.primary_artist == "Foo Fighters"
    assert everlong.album == "The Colour and the Shape"
    # iTunes files have no playlist column: filename stem is the label.
    assert everlong.playlists == ["itunes_playlist"]


def test_itunes_txt_empty_album_kept_empty():
    from csv_to_clonehero.csv_import import import_csv_file

    tracks = import_csv_file(ITUNES_FIXTURES / "itunes_playlist.txt")
    loose = next(t for t in tracks if t.title == "Loose Track")
    assert loose.album == ""
    assert loose.album_norm == ""


def test_itunes_xml_import():
    tracks = import_itunes_xml(ITUNES_FIXTURES / "itunes_library.xml")
    by_title = {t.title: t for t in tracks}
    # The row with empty Name/Artist is skipped.
    assert set(by_title) == {"Everlong", "Midnight City", "Untitled Bonus"}
    everlong = by_title["Everlong"]
    assert everlong.primary_artist == "Foo Fighters"
    assert everlong.album == "The Colour and the Shape"
    assert everlong.playlists == ["itunes_library"]


def test_itunes_xml_album_artist_fallback():
    tracks = import_itunes_xml(ITUNES_FIXTURES / "itunes_library.xml")
    bonus = next(t for t in tracks if t.title == "Untitled Bonus")
    assert bonus.primary_artist == "Various Artists"


def test_itunes_xml_dir_import_merges_with_txt():
    # Both fixture files import together; identical songs dedupe across formats.
    result = import_playlist_dir(ITUNES_FIXTURES)
    everlongs = [t for t in result.tracks if t.title == "Everlong"]
    assert len(everlongs) == 1
    assert set(everlongs[0].playlists) == {"itunes_playlist", "itunes_library"}


def test_itunes_xml_rejects_non_plist(tmp_path):
    bad = tmp_path / "bad.xml"
    bad.write_text("<html>not a plist</html>", encoding="utf-8")
    with pytest.raises(ValueError, match="not a valid iTunes XML"):
        import_itunes_xml(bad)


def test_itunes_xml_rejects_plist_without_tracks(tmp_path):
    bad = tmp_path / "empty.xml"
    bad.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<plist version="1.0"><dict><key>Name</key><string>x</string></dict></plist>',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="no Tracks dict"):
        import_itunes_xml(bad)


# ------------------------------------------------------------ CSV hardening
def test_csv_with_utf8_bom(tmp_path):
    path = tmp_path / "bom.csv"
    path.write_bytes("Track Name,Artist Name,Album\nSong X,Artist Y,Album Z\n".encode("utf-8-sig"))
    result = import_playlist_dir(tmp_path)
    track = result.tracks[0]
    assert track.title == "Song X"
    assert track.primary_artist == "Artist Y"
    assert track.album == "Album Z"


def test_csv_with_cp1252_encoding(tmp_path):
    path = tmp_path / "legacy.csv"
    # cp1252 smart quotes are invalid UTF-8 and must fall back cleanly.
    path.write_bytes("Song,Artist\nCaf\xe9 Delight,Bj\xf6rk\n".encode("cp1252"))
    result = import_playlist_dir(tmp_path)
    track = result.tracks[0]
    assert track.title == "Café Delight"
    assert track.primary_artist == "Björk"


def test_tab_delimited_sniffed_without_txt_extension(tmp_path):
    path = tmp_path / "weird.csv"  # .csv name, tab-delimited content
    path.write_text("Name\tArtist\tAlbum\nSong\tArtist\tAlbum\n", encoding="utf-8")
    result = import_playlist_dir(tmp_path)
    assert result.tracks[0].title == "Song"
    assert result.tracks[0].album == "Album"
