from csv_to_clonehero.normalization import (
    normalize_artist,
    normalize_text,
    normalize_title,
    split_artists,
    strip_version_noise,
    version_markers,
)


def test_remaster_suffix_matches_plain_title():
    assert normalize_title("Everlong - 2015 Remaster") == normalize_title("Everlong")


def test_remaster_suffix_with_comma_title():
    assert normalize_title("Paint It, Black - 2009 Remaster") == normalize_title("Paint It, Black")


def test_parenthesized_remaster_stripped():
    assert normalize_title("Kashmir (Remastered)") == normalize_title("Kashmir")
    assert normalize_title("Kashmir (2015 Remaster)") == normalize_title("Kashmir")


def test_deluxe_and_edition_noise():
    assert normalize_title("Song - Deluxe Edition") == normalize_title("Song")
    assert normalize_title("Song (25th Anniversary Edition)") == normalize_title("Song")


def test_album_single_radio_versions():
    assert normalize_title("Song - Album Version") == normalize_title("Song")
    assert normalize_title("Song (Single Version)") == normalize_title("Song")
    assert normalize_title("Song - Radio Edit") == normalize_title("Song")


def test_meaningful_parentheses_preserved():
    result = normalize_title("(Don't Fear) The Reaper")
    assert "don t fear" in result.replace("'", " ") or "don't fear" in result or "dont fear" in result
    assert result != "the reaper"


def test_live_and_remix_not_stripped():
    # These identify a different *version* and must survive normalization.
    assert normalize_title("Song - Live at Wembley") != normalize_title("Song")
    assert normalize_title("Song (Remix)") != normalize_title("Song")


def test_version_markers_detected():
    assert "live" in version_markers("Song - Live at Wembley")
    assert "remix" in version_markers("Song (Club Remix)")
    assert version_markers("Song") == set()


def test_unicode_apostrophes_and_dashes():
    assert normalize_text("Don\u2019t Stop \u2013 Believin\u2019") == normalize_text("Don't Stop - Believin'")


def test_capitalization_and_whitespace():
    assert normalize_text("  Paint   It,  BLACK ") == normalize_text("paint it black")


def test_artist_normalization():
    assert normalize_artist("The Rolling Stones") == normalize_artist("the rolling stones")


def test_split_artists_comma():
    primary, featured = split_artists("Artist A, Artist B")
    assert primary == "Artist A"
    assert featured == ["Artist B"]


def test_split_artists_featuring():
    primary, featured = split_artists("Gorillaz feat. De La Soul")
    assert primary == "Gorillaz"
    assert featured == ["De La Soul"]


def test_split_artists_single():
    primary, featured = split_artists("Foo Fighters")
    assert primary == "Foo Fighters"
    assert featured == []


def test_strip_version_noise_keeps_plain_titles():
    assert strip_version_noise("Everlong") == "Everlong"
