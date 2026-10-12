"""Tiny independently generated SNGPKG/WAV fixtures; no provider media."""
from __future__ import annotations

import io
import math
import struct
import wave


def wav_audio(duration: float = 0.25) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"".join(struct.pack("<h", int(2000 * math.sin(index * math.tau * 440 / 8000)))
            for index in range(int(duration * 8000))))
    return out.getvalue()


def constant_wav(segments, rate=22050):
    """Segments are (seconds, signed PCM amplitude), useful for exact mix checks."""
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(rate)
        wav.writeframes(b"".join(struct.pack("<h", amplitude) * round(seconds * rate)
            for seconds, amplitude in segments))
    return out.getvalue()


def sng_audio(files=None, metadata=None) -> bytes:
    files = files if files is not None else [("notes.chart", b"synthetic notes"), ("song.wav", wav_audio())]
    metadata = metadata if metadata is not None else {"name": "Synthetic song", "artist": "Offline fixture", "preview_start_time": "25"}
    pairs = bytearray()
    for key, value in metadata.items():
        for part in (key.encode(), str(value).encode()):
            pairs.extend(struct.pack("<i", len(part))); pairs.extend(part)
    mask = bytes((index * 11) % 256 for index in range(16))
    index_size = sum(1 + len(name.encode()) + 16 for name, _ in files)
    data_start = 26 + 16 + len(pairs) + 16 + index_size + 8
    entries, masked = bytearray(), bytearray()
    offset = data_start
    for name, data in files:
        raw = name.encode()
        entries.extend(bytes([len(raw)]) + raw + struct.pack("<QQ", len(data), offset))
        masked.extend(byte ^ mask[index % 16] ^ (index & 255) for index, byte in enumerate(data))
        offset += len(data)
    return b"SNGPKG" + struct.pack("<I", 1) + mask + struct.pack("<QQ", 8 + len(pairs), len(metadata)) + pairs \
        + struct.pack("<QQ", 8 + len(entries), len(files)) + entries + struct.pack("<Q", len(masked)) + masked
