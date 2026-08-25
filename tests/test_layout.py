"""Library folder layouts: hybrid / artist_album / pack / legacy."""

from csv_to_clonehero.filesystem import MAX_RELPATH_CHARS, destination_path
from csv_to_clonehero.models import SINGLES_ALBUM_LABEL

from .conftest import make_chart, make_track


def test_hybrid_exact_uses_track_artist_album(config):
    track = make_track("Everlong", "Foo Fighters", album="The Colour and the Shape")
    chart = make_chart("Everlong", "Foo Fighters", chart_id=1)
    relpath = destination_path(config, chart, "exact", track=track)
    parts = relpath.parts
    assert parts[0] == "Foo Fighters"
    assert parts[1] == "The Colour and the Shape"
    assert parts[2].startswith("Foo Fighters - Everlong [TestCharter]")
    assert parts[2].endswith(".sng")


def test_hybrid_exact_missing_album_goes_to_singles(config):
    track = make_track("Loose Song", "Solo Artist")
    chart = make_chart("Loose Song", "Solo Artist", chart_id=1)
    relpath = destination_path(config, chart, "exact", track=track)
    assert relpath.parts[1] == SINGLES_ALBUM_LABEL


def test_hybrid_exact_chart_album_is_fallback(config):
    track = make_track("Song", "Artist")  # no track album
    chart = make_chart("Song", "Artist", chart_id=1)
    chart.album = "Chart Album"
    relpath = destination_path(config, chart, "exact", track=track)
    assert relpath.parts[1] == "Chart Album"


def test_hybrid_expansion_uses_pack_folder(config):
    chart = make_chart("Deep Cut", "Big Artist", chart_id=1)
    chart.pack_name = "Rock Legends Vol. 1"
    relpath = destination_path(config, chart, "expansion")
    assert relpath.parts[0] == "Rock Legends Vol. 1"
    assert relpath.parts[1].startswith("Big Artist - Deep Cut")


def test_hybrid_expansion_empty_pack_falls_back_to_artist(config):
    chart = make_chart("Deep Cut", "Big Artist", chart_id=1)
    relpath = destination_path(config, chart, "expansion")
    assert relpath.parts[0] == "Big Artist"
    assert len(relpath.parts) == 2


def test_artist_album_layout_applies_to_expansion(config):
    config.library.layout = "artist_album"
    chart = make_chart("Deep Cut", "Big Artist", chart_id=1)
    chart.pack_name = "Some Pack"
    chart.album = "Album X"
    relpath = destination_path(config, chart, "expansion")
    assert relpath.parts[0] == "Big Artist"
    assert relpath.parts[1] == "Album X"


def test_pack_layout_applies_to_exact(config):
    config.library.layout = "pack"
    track = make_track("Song", "Artist", album="Album")
    chart = make_chart("Song", "Artist", chart_id=1)
    chart.pack_name = "Pack Y"
    relpath = destination_path(config, chart, "exact", track=track)
    assert relpath.parts[0] == "Pack Y"


def test_legacy_layout_keeps_root_and_section(config):
    config.library.layout = "legacy"
    config.hierarchy.root_folder = "My Root"
    chart = make_chart("Song", "Artist", chart_id=1)
    exact = destination_path(config, chart, "exact", track=make_track("Song", "Artist"))
    expansion = destination_path(config, chart, "expansion")
    assert exact.parts[:3] == ("My Root", "Exact Playlist Songs", "Artist")
    assert expansion.parts[:3] == ("My Root", "Artist Expansion", "Artist")


def test_reserved_name_album_sanitized(config):
    track = make_track("Song", "Artist", album="CON")
    chart = make_chart("Song", "Artist", chart_id=1)
    relpath = destination_path(config, chart, "exact", track=track)
    assert relpath.parts[1] == "_CON"


def test_long_names_truncate_title_first(config):
    artist = "A" * 40
    album = "B" * 40
    title = "T" * 200
    track = make_track(title, artist, album=album)
    chart = make_chart(title, artist, chart_id=1, charter="C" * 10)
    relpath = destination_path(config, chart, "exact", track=track)
    # Artist and album folders stay intact; the whole path fits the budget.
    assert relpath.parts[0] == artist
    assert relpath.parts[1] == album
    assert len(str(relpath)) <= MAX_RELPATH_CHARS
    assert relpath.parts[2].endswith(".sng")
    assert "T" * 100 not in relpath.parts[2]  # title was truncated


def test_charter_tag_dropped_before_starving_title(config):
    artist = "A" * 60
    album = "B" * 60
    title = "T" * 60
    charter = "C" * 40
    track = make_track(title, artist, album=album)
    chart = make_chart(title, artist, chart_id=1, charter=charter)
    relpath = destination_path(config, chart, "exact", track=track)
    assert relpath.parts[0] == artist
    assert relpath.parts[1] == album
    # No room for the charter tag; the title keeps a usable length.
    assert charter not in relpath.parts[2]


def test_filename_component_never_exceeds_cap_including_sng_suffix(config):
    from csv_to_clonehero.filesystem import MAX_COMPONENT_LENGTH

    cases = [
        ("T" * 180, "LMBECIL1832"),          # very long title + long charter
        ("X" * 30, "C" * 60),                 # long charter dominates
        ("Another long song title" * 8, "Short"),
    ]
    for title, charter in cases:
        artist = "Tyler, The Creator"
        album = "Flower Boy"
        track = make_track(title, artist, album=album)
        chart = make_chart(title, artist, chart_id=1, charter=charter)
        relpath = destination_path(config, chart, "exact", track=track)
        filename = relpath.parts[-1]
        assert filename.endswith(".sng")
        # The full filename component, including ".sng", must fit the cap that
        # the exporter enforces at write time.
        assert len(filename) <= MAX_COMPONENT_LENGTH, filename
