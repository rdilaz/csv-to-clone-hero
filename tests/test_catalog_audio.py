"""Provider-free bounded SNG audio loading, cancellation, and container safety."""
from __future__ import annotations
import base64
import json
import shutil
import struct
import subprocess
from pathlib import Path
import pytest
from .catalog_audio_fixtures import sng_audio, wav_audio

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node") or str(Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe")
SCRIPT = ROOT / "src/csv_to_clonehero/server/static/catalog-audio.js"
HARNESS = ROOT / "tests/catalog_audio_harness.cjs"


def run(package=None, **options):
    payload = {"package": base64.b64encode(package if package is not None else sng_audio()).decode(), **options}
    result = subprocess.run([NODE, str(HARNESS), str(SCRIPT)], input=json.dumps(payload), text=True,
        capture_output=True, timeout=20, check=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize("hidden_range", [False, True])
def test_loader_decodes_exact_wav_and_releases_its_one_blob(hidden_range):
    result = run(hiddenContentRange=hidden_range, item={"has_video_background": True})
    assert result["ok"] is True, result
    assert base64.b64decode(result["audio"]) == wav_audio()
    assert result["mime"] == "audio/wav" and result["durationHint"] == 1000
    assert result["previewStartMs"] == 25
    assert result["created"] == result["revoked"] == 1
    assert all(request["url"] == "https://files.enchor.us/" + "a" * 32 + "_novideo.sng" for request in result["requests"])
    assert all(request["credentials"] == "omit" and request["redirect"] == "error" for request in result["requests"])
    assert result["progress"][-1] == "Chart audio ready"


@pytest.mark.parametrize("item", [{"md5": "../secret"}, {"md5": "A" * 32}, {"source": "unknown"}, {"md5": 123}])
def test_unverified_identity_never_requests_any_url(item):
    result = run(item=item)
    assert not result["ok"] and result["requests"] == [] and result["created"] == 0


@pytest.mark.parametrize("status", [200, 404, 429, 500])
def test_status_failures_never_fallback_to_full_package(status):
    result = run(status=status)
    assert not result["ok"] and len(result["requests"]) == 1 and result["created"] == 0


@pytest.mark.parametrize("options", [{"badRange": True}, {"truncateAt": 1}, {"overrunAt": 1, "omitContentLength": True}])
def test_unexpected_response_ranges_are_rejected(options):
    result = run(**options)
    assert not result["ok"] and result["created"] == 0


@pytest.mark.parametrize("abort_at", [1, 2, 3, 6])
def test_cancellation_leaves_no_playable_or_leaked_blob(abort_at):
    result = run(abortAt=abort_at)
    assert not result["ok"] and result["name"] == "AbortError" and result["created"] == 0


def test_cancellation_at_final_progress_never_returns_a_stale_url():
    result = run(abortAtReady=True)
    assert not result["ok"] and result["name"] == "AbortError" and result["created"] == 0


@pytest.mark.parametrize("files,reason", [
    ([("notes.chart", b"notes")], "no supported"),
    ([("guitar.wav", wav_audio())], "cannot decode combined"),
    ([("song.wav", wav_audio()), ("guitar.wav", wav_audio())], "cannot decode combined"),
    ([("song.wav", wav_audio()), ("guitar.flac", b"fLaC")], "unsupported stem codec"),
    ([("song.wav", b"not audio")], "valid WAV"),
    ([("song.wav", wav_audio()), ("SONG.WAV", wav_audio())], "duplicate"),
    ([("../song.wav", wav_audio())], "path is invalid"),
])
def test_no_audio_bad_audio_or_partial_stems_are_not_presented_as_full_mix(files, reason):
    result = run(sng_audio(files))
    assert not result["ok"] and reason in result["error"] and result["created"] == 0


@pytest.mark.parametrize("change,reason", [
    (lambda b: b.__setitem__(slice(0, 6), b"INVALID"[:6]), "unsupported"),
    (lambda b: struct.pack_into("<I", b, 6, 2), "version"),
    (lambda b: struct.pack_into("<Q", b, 26, 256 * 1024 + 1), "limit"),
    (lambda b: struct.pack_into("<Q", b, 34, 513), "limit"),
])
def test_malformed_and_oversized_package_headers_stop_before_audio(change, reason):
    package = bytearray(sng_audio()); change(package)
    result = run(bytes(package), hiddenContentRange=True)
    assert not result["ok"] and reason in result["error"] and len(result["requests"]) == 1


def test_member_offset_overlap_rejected_before_any_audio_request():
    package = bytearray(sng_audio())
    metadata_length = struct.unpack_from("<Q", package, 26)[0]
    index_head = 34 + metadata_length
    first = index_head + 16
    name_length = package[first]
    struct.pack_into("<Q", package, first + 1 + name_length + 8, 0)
    result = run(bytes(package))
    assert not result["ok"] and "out-of-bounds" in result["error"]
    assert len(result["requests"]) == 5 and result["created"] == 0


def test_large_member_remains_byte_exact_across_bounded_reads():
    audio = wav_audio(70)
    result = run(sng_audio([("song.wav", audio)]), hiddenContentRange=True)
    assert result["ok"] is True, result
    assert base64.b64decode(result["audio"]) == audio
    assert len(result["requests"]) == 7


def test_declared_oversize_audio_is_rejected_before_member_transfer():
    package = bytearray(sng_audio([("song.wav", wav_audio())]))
    meta_length = struct.unpack_from("<Q", package, 26)[0]
    index_start = 34 + meta_length
    index_length = struct.unpack_from("<Q", package, index_start)[0]
    entry = index_start + 16
    struct.pack_into("<Q", package, entry + 1 + package[entry], 24 * 1024 * 1024 + 1)
    struct.pack_into("<Q", package, index_start + 8 + index_length, 24 * 1024 * 1024 + 1)
    result = run(bytes(package), hiddenContentRange=True)
    assert not result["ok"] and "24 MiB" in result["error"]
    assert len(result["requests"]) == 5 and result["created"] == 0


def _stem_package():
    return sng_audio([("song.wav", wav_audio()), ("guitar.wav", wav_audio())])


@pytest.mark.parametrize("duration", [None, 0, -1, 600001])
def test_unknown_or_long_stem_duration_never_fetches_audio(duration):
    result = run(_stem_package(), item={"song_length_ms": duration})
    assert not result["ok"] and "known song duration" in result["error"]
    assert len(result["requests"]) == 5 and result["created"] == 0


def test_unknown_audio_name_is_not_silently_omitted_from_mix():
    result = run(sng_audio([("song.wav", wav_audio()), ("mystery.wav", wav_audio())]))
    assert not result["ok"] and "unknown audio file" in result["error"] and len(result["requests"]) == 5


def test_stem_count_bound_rejects_before_any_audio_transfer():
    names = ["song", "guitar", "bass", "rhythm", "keys", "vocals", "vocals_1", "vocals_2", "drums", "drums_1", "drums_2", "drums_3", "drums_4"]
    result = run(sng_audio([(name + ".wav", wav_audio()) for name in names]))
    assert not result["ok"] and "12-stem" in result["error"] and len(result["requests"]) == 5


@pytest.mark.parametrize("shape", [{"frames": 600 * 22050 + 1}, {"frames": 100, "channels": 3}, {"frames": 100, "sampleRate": 48000}])
def test_decoded_frame_and_channel_bounds_stop_before_second_stem(shape):
    result = run(_stem_package(), decoded=[shape])
    assert not result["ok"] and "decoded chart stem" in result["error"]
    assert result["decodes"] == result["contextsClosed"] == 1
    assert len(result["requests"]) == 6 and result["created"] == 0


@pytest.mark.parametrize("options,reason", [({"abortDuringDecode": True}, "AbortError"), ({"decodeError": True}, "could not decode every")])
def test_cancel_or_codec_failure_discards_partial_mix_and_closes_context(options, reason):
    result = run(_stem_package(), decoded=[{"frames": 22050}], **options)
    assert not result["ok"] and (reason == result["name"] or reason in result["error"])
    assert result["contextsClosed"] == 1 and result["created"] == 0 and len(result["requests"]) == 6


def test_mixed_clip_is_capped_uses_source_offset_and_seeks_output_from_zero():
    result = run(_stem_package(), item={"song_length_ms": 120000, "preview_start_ms": 30000},
        decoded=[{"frames": 120 * 22050, "value": 0.1}, {"frames": 120 * 22050, "value": 0.2}])
    assert result["ok"] is True, result
    assert result["previewKind"] == "mixed-clip" and result["previewStartMs"] == 0
    assert result["clip_start_ms"] == 30000 and result["durationHint"] == 45000
    assert result["decodes"] == 2 and result["contextsClosed"] == 1
    audio = base64.b64decode(result["audio"])
    assert struct.unpack_from("<I", audio, 24)[0] == 22050
    assert struct.unpack_from("<I", audio, 40)[0] == 45 * 22050 * 2 * 2
    assert struct.unpack_from("<h", audio, 44)[0] == pytest.approx(round(0.3 / 2 ** 0.5 * 32767), abs=2)
