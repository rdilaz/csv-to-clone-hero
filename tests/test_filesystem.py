import zipfile

import pytest

from csv_to_clonehero.filesystem import (
    is_within,
    safe_extract_zip,
    sanitize_component,
    sanitize_relative_path,
)


@pytest.mark.parametrize("char", '<>:"/\\|?*')
def test_invalid_chars_replaced(char):
    result = sanitize_component(f"AC{char}DC")
    assert char not in result
    assert result


@pytest.mark.parametrize("name", ["CON", "con", "PRN", "COM1", "LPT9", "NUL"])
def test_reserved_names_prefixed(name):
    result = sanitize_component(name)
    assert result.upper() not in {"CON", "PRN", "NUL", "COM1", "LPT9"}
    assert result.lstrip("_").upper() == name.upper()


def test_trailing_dots_and_spaces_removed():
    assert sanitize_component("Artist...") == "Artist"
    assert sanitize_component("  Artist  ") == "Artist"


def test_empty_falls_back():
    assert sanitize_component("???") != ""
    assert sanitize_component("") == "Unknown"


def test_long_names_truncated():
    result = sanitize_component("A" * 200)
    assert len(result) <= 80


def test_real_artist_names_survive():
    assert sanitize_component("Blue Öyster Cult") == "Blue Öyster Cult"
    assert sanitize_component("AC/DC") == "AC_DC"
    assert sanitize_component("(Don't Fear) The Reaper") == "(Don't Fear) The Reaper"


def test_sanitize_relative_path():
    path = sanitize_relative_path("00 - His Music", "Exact Playlist Songs", "AC/DC")
    assert str(path).endswith("AC_DC")


def _make_zip(path, members: dict[str, bytes]):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)


def test_safe_extract_ok(tmp_path):
    archive = tmp_path / "chart.zip"
    _make_zip(archive, {"Song/notes.chart": b"x", "Song/song.ini": b"y"})
    dest = tmp_path / "out"
    extracted = safe_extract_zip(archive, dest)
    assert len(extracted) == 2
    assert (dest / "Song" / "notes.chart").exists()


def test_safe_extract_rejects_traversal(tmp_path):
    archive = tmp_path / "evil.zip"
    _make_zip(archive, {"../../evil.txt": b"x"})
    with pytest.raises(ValueError):
        safe_extract_zip(archive, tmp_path / "out")
    assert not (tmp_path / "evil.txt").exists()


def test_safe_extract_rejects_absolute(tmp_path):
    archive = tmp_path / "evil2.zip"
    _make_zip(archive, {"/abs/path.txt": b"x"})
    with pytest.raises(ValueError):
        safe_extract_zip(archive, tmp_path / "out")


def test_is_within(tmp_path):
    parent = tmp_path / "library"
    assert is_within(parent / "a" / "b.sng", parent)
    assert not is_within(tmp_path / "elsewhere" / "b.sng", parent)
