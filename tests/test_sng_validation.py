from __future__ import annotations

import io
import struct
import zipfile

from csv_to_clonehero.exporter import validate_sng


def _midi(*, paired: bool = True, marker_only: bool = False) -> bytes:
    note = 103 if marker_only else 96
    events = b"\x00\xff\x03\x0bPART GUITAR"
    events += b"\x00\x90" + bytes([note, 100])
    if paired:
        events += b"\x0a\x80" + bytes([note, 0])
    events += b"\x00\xff\x2f\x00"
    return (
        b"MThd"
        + struct.pack(">IHHH", 6, 1, 1, 480)
        + b"MTrk"
        + struct.pack(">I", len(events))
        + events
    )


def _zip_chart(chart_data: bytes) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("song.ini", "[song]\nname = Sample\n")
        archive.writestr("notes.mid", chart_data)
    return payload.getvalue()


def _sngpkg(chart_data: bytes) -> bytes:
    def pair(key: str, value: str) -> bytes:
        key_bytes = key.encode("utf-8")
        value_bytes = value.encode("utf-8")
        return (
            struct.pack("<i", len(key_bytes))
            + key_bytes
            + struct.pack("<i", len(value_bytes))
            + value_bytes
        )

    metadata = b"".join(
        pair(key, value)
        for key, value in (("name", "Sample"), ("artist", "Artist"))
    )
    mask = bytes(range(16))
    files = [("notes.mid", chart_data), ("guitar.opus", b"audio")]
    index_length = sum(1 + len(name.encode("utf-8")) + 16 for name, _ in files)
    data_start = 26 + 16 + len(metadata) + 16 + index_length + 8
    index = bytearray()
    encoded: list[bytes] = []
    offset = data_start
    for name, plain in files:
        masked = bytes(
            value ^ mask[position % 16] ^ (position % 256)
            for position, value in enumerate(plain)
        )
        encoded.append(masked)
        name_bytes = name.encode("utf-8")
        index.extend(bytes([len(name_bytes)]))
        index.extend(name_bytes)
        index.extend(struct.pack("<QQ", len(masked), offset))
        offset += len(masked)
    return (
        b"SNGPKG"
        + struct.pack("<I", 1)
        + mask
        + struct.pack("<QQ", 8 + len(metadata), 2)
        + metadata
        + struct.pack("<QQ", 8 + len(index), len(files))
        + index
        + struct.pack("<Q", sum(map(len, encoded)))
        + b"".join(encoded)
    )


def test_validate_zip_requires_actual_playable_chart_notes(tmp_path):
    valid = tmp_path / "valid.sng"
    valid.write_bytes(_zip_chart(_midi()))
    result = validate_sng(valid, require_five_fret_guitar=True)
    assert result.valid
    assert result.format == "zip/midi"
    assert result.chart_member == "notes.mid"
    assert result.playable_note_count == 1
    assert result.note_counts == (("guitar:expert", 1),)

    marker_only = tmp_path / "marker.sng"
    marker_only.write_bytes(_zip_chart(_midi(marker_only=True)))
    assert not validate_sng(marker_only).valid


def test_validate_midi_rejects_unpaired_note_events(tmp_path):
    path = tmp_path / "unpaired.sng"
    path.write_bytes(_zip_chart(_midi(paired=False)))
    result = validate_sng(path)
    assert not result.valid
    assert "playable notes" in result.error


def test_validate_sngpkg_decodes_chart_member_without_song_ini(tmp_path):
    path = tmp_path / "package.sng"
    path.write_bytes(_sngpkg(_midi()))
    result = validate_sng(path, require_five_fret_guitar=True)
    assert result.valid, result.error
    assert result.format == "sngpkg/midi"
    assert result.playable_note_count == 1
    assert result.chart_member == "notes.mid"


def test_validate_sngpkg_rejects_truncation_and_zero_notes(tmp_path):
    truncated = tmp_path / "truncated.sng"
    package = _sngpkg(_midi())
    truncated.write_bytes(package[:-1])
    assert not validate_sng(truncated).valid

    zero = tmp_path / "zero.sng"
    zero.write_bytes(_sngpkg(_midi(marker_only=True)))
    result = validate_sng(zero)
    assert not result.valid
    assert "playable notes" in result.error
