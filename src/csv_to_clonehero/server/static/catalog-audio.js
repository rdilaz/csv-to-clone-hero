/* Independently authored SNGPKG v1 reader from the published MIT SNG specification.
 * https://github.com/Geomitron/SngFileFormat/tree/1b3b9d6e02580bd2ab973bdd5ea4a6d4b6017bb7
 * Reads bounded audio members directly from Enchor; never extracts paths or video.
 */
(() => {
  "use strict";
  const LIMITS = Object.freeze({metadataBytes: 256 * 1024, indexBytes: 256 * 1024,
    metadataEntries: 512, members: 1024, packageBytes: 1024 * 1024 * 1024,
    audioBytes: 24 * 1024 * 1024, chunkBytes: 1024 * 1024, requestTimeoutMs: 15000,
    stems: 12, sourceDurationMs: 600000, clipMs: 45000, sampleRate: 22050,
    decodedFrames: 600 * 22050, decodedChannels: 2});
  const decoder = new TextDecoder("utf-8", {fatal: true});
  const AUDIO = /\.(ogg|opus|mp3|wav|flac|m4a|aac|aif|aiff)$/i;
  const MIME = {ogg: "audio/ogg", opus: "audio/ogg; codecs=opus", mp3: "audio/mpeg", wav: "audio/wav"};
  let mixerQueue = Promise.resolve();

  function aborted(signal) {
    if (signal?.aborted) throw new DOMException("Audio loading cancelled", "AbortError");
  }
  function fail(message) { throw new Error(message); }
  function uint64(bytes, offset, label, maximum = LIMITS.packageBytes) {
    if (offset < 0 || offset + 8 > bytes.length) fail(`The chart ${label} is truncated.`);
    const value = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength).getBigUint64(offset, true);
    if (value > BigInt(maximum)) fail(`The chart ${label} exceeds the audio preview limit.`);
    return Number(value);
  }
  function text(bytes, label) {
    let value;
    try { value = decoder.decode(bytes); } catch (_) { fail(`The chart ${label} is not valid UTF-8.`); }
    if (/[\x00-\x1f\x7f]/.test(value)) fail(`The chart ${label} contains invalid text.`);
    return value;
  }
  function nonnegative(value) {
    if (value === null || value === undefined || value === "") return null;
    const number = Number(value);
    return Number.isFinite(number) && number >= 0 ? number : null;
  }
  function metadata(bytes, count) {
    const values = Object.create(null);
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    let cursor = 0;
    function string(label) {
      if (cursor + 4 > bytes.length) fail("The chart metadata is truncated.");
      const size = view.getInt32(cursor, true); cursor += 4;
      if (size < 0 || size > 65536 || cursor + size > bytes.length) fail("The chart metadata length is invalid.");
      const result = text(bytes.subarray(cursor, cursor + size), label); cursor += size;
      return result;
    }
    for (let i = 0; i < count; i++) {
      const key = string("metadata key");
      if (!key || Object.hasOwn(values, key)) fail("The chart has duplicate or empty metadata keys.");
      values[key] = string("metadata value");
    }
    if (cursor !== bytes.length) fail("The chart metadata has unexpected trailing bytes.");
    return values;
  }
  function index(bytes, count) {
    const entries = [], seen = new Set();
    let cursor = 0;
    for (let i = 0; i < count; i++) {
      if (cursor >= bytes.length) fail("The chart audio index is truncated.");
      const size = bytes[cursor++];
      if (!size || cursor + size + 16 > bytes.length) fail("The chart member name is invalid.");
      const name = text(bytes.subarray(cursor, cursor + size), "member name"); cursor += size;
      if (name.startsWith("/") || name.includes("\\") || name.includes(":")
          || name.split("/").some(part => !part || part === "." || part === "..")) fail("The chart member path is invalid.");
      const key = name.toLowerCase();
      if (seen.has(key)) fail("The chart has duplicate file names.");
      seen.add(key);
      const sizeBytes = uint64(bytes, cursor, "member size"); cursor += 8;
      const offset = uint64(bytes, cursor, "member offset"); cursor += 8;
      entries.push({name, size: sizeBytes, offset});
    }
    if (cursor !== bytes.length) fail("The chart audio index has unexpected trailing bytes.");
    return entries;
  }
  function selectAudio(entries) {
    const audio = entries.filter(entry => AUDIO.test(entry.name)
      && !/(?:^|\/)(?:preview|crowd)(?:_[^.]*)?\./i.test(entry.name));
    if (!audio.length) fail("This chart package has no supported song audio preview.");
    if (audio.length > LIMITS.stems) fail("This chart exceeds the 12-stem preview limit.");
    if (audio.some(entry => !/^(song|guitar|bass|rhythm|keys|vocals(?:_[12])?|drums(?:_[1-4])?)\.(ogg|opus|mp3|wav)$/i.test(entry.name))) {
      fail("This chart has an unknown audio file or unsupported stem codec. A complete preview cannot be confirmed.");
    }
    if (audio.some(entry => entry.size < 4) || audio.reduce((sum, entry) => sum + entry.size, 0) > LIMITS.audioBytes) {
      fail("This song audio exceeds the 24 MiB preview limit or is empty.");
    }
    return {entries: audio, single: audio.length === 1 && /^song\./i.test(audio[0].name)};
  }
  function validateAudio(bytes, extension) {
    const matches = (offset, value) => Array.from(value).every((char, i) => bytes[offset + i] === char.charCodeAt(0));
    if ((extension === "ogg" || extension === "opus") && !matches(0, "OggS")) fail("The song audio is not a valid Ogg container.");
    if (extension === "wav" && (bytes.length < 44 || !matches(0, "RIFF") || !matches(8, "WAVE"))) fail("The song audio is not a valid WAV container.");
    if (extension === "mp3" && !(matches(0, "ID3") || (bytes[0] === 255 && (bytes[1] & 224) === 224))) fail("The song audio is not a valid MP3 container.");
  }

  async function readAudio(entry, url, mask, total, signal, onProgress, label) {
    const audio = new Uint8Array(entry.size);
    for (let cursor = 0; cursor < entry.size; cursor += LIMITS.chunkBytes) {
      aborted(signal);
      onProgress(`${label} (${Math.floor(cursor / entry.size * 100)}%)`);
      const bytes = await range(url, entry.offset + cursor, Math.min(LIMITS.chunkBytes, entry.size - cursor), signal, total);
      for (let i = 0; i < bytes.length; i++) audio[cursor + i] = bytes[i] ^ mask[(cursor + i) % 16] ^ ((cursor + i) & 255);
    }
    validateAudio(audio, entry.name.toLowerCase().split(".").pop());
    return audio;
  }

  function wavClip(channels, frames) {
    const bytes = new Uint8Array(44 + frames * channels.length * 2);
    const view = new DataView(bytes.buffer);
    const word = (offset, value) => Array.from(value).forEach((char, i) => {bytes[offset + i] = char.charCodeAt(0);});
    word(0, "RIFF"); view.setUint32(4, bytes.length - 8, true); word(8, "WAVE");
    word(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
    view.setUint16(22, channels.length, true); view.setUint32(24, LIMITS.sampleRate, true);
    view.setUint32(28, LIMITS.sampleRate * channels.length * 2, true);
    view.setUint16(32, channels.length * 2, true); view.setUint16(34, 16, true);
    word(36, "data"); view.setUint32(40, bytes.length - 44, true);
    let cursor = 44;
    for (let frame = 0; frame < frames; frame++) {
      for (const channel of channels) {
        const sample = Math.max(-1, Math.min(1, channel[frame]));
        view.setInt16(cursor, Math.round(sample * (sample < 0 ? 32768 : 32767)), true); cursor += 2;
      }
    }
    return bytes;
  }

  async function mixedClip(entries, url, mask, total, duration, previewStart, signal, onProgress) {
    if (duration === null || duration <= 0 || duration > LIMITS.sourceDurationMs) {
      fail("Stem previews require a known song duration of at most 10 minutes.");
    }
    const Context = window.OfflineAudioContext || window.webkitOfflineAudioContext;
    if (!Context) fail("This browser cannot decode combined chart audio previews.");
    // Serialise mixers across rapid skips as native decodeAudioData cannot be
    // interrupted. A stale decode is discarded before any further stem fetch.
    const previous = mixerQueue;
    let release;
    mixerQueue = new Promise(resolve => {release = resolve;});
    await previous;
    let context = null, clip = null, decoded = null, encoded = null;
    try {
      aborted(signal);
      context = new Context(2, 1, LIMITS.sampleRate);
      const clipStart = Math.min(previewStart, Math.max(0, duration - 1));
      const startFrame = Math.floor(clipStart * LIMITS.sampleRate / 1000);
      const maximumFrames = Math.max(1, Math.ceil(Math.min(LIMITS.clipMs, duration - clipStart) * LIMITS.sampleRate / 1000));
      clip = [new Float32Array(maximumFrames), new Float32Array(maximumFrames)];
      const gain = 1 / Math.sqrt(entries.length);
      let usedFrames = 0;
      for (let i = 0; i < entries.length; i++) {
        aborted(signal);
        encoded = await readAudio(entries[i], url, mask, total, signal, onProgress, `Loading chart stem ${i + 1} of ${entries.length}`);
        onProgress(`Combining chart stem ${i + 1} of ${entries.length}`);
        aborted(signal);
        try { decoded = await context.decodeAudioData(encoded.buffer); }
        catch (_) { aborted(signal); fail("This browser could not decode every chart audio stem. No partial mix will be played."); }
        encoded = null;
        aborted(signal);
        if (!decoded || decoded.sampleRate !== LIMITS.sampleRate || !Number.isSafeInteger(decoded.length)
            || decoded.length < 1 || decoded.length > LIMITS.decodedFrames
            || decoded.numberOfChannels < 1 || decoded.numberOfChannels > LIMITS.decodedChannels) {
          fail("A decoded chart stem exceeds the 10-minute stereo preview limit.");
        }
        const available = Math.min(maximumFrames, Math.max(0, decoded.length - startFrame));
        usedFrames = Math.max(usedFrames, available);
        for (let channel = 0; channel < 2; channel++) {
          const input = decoded.getChannelData(Math.min(channel, decoded.numberOfChannels - 1));
          for (let frame = 0; frame < available; frame++) {
            const sample = input[startFrame + frame];
            if (!Number.isFinite(sample)) fail("A chart stem contains invalid audio samples.");
            clip[channel][frame] += sample * gain;
          }
        }
        decoded = null;
        // Yield between native decodes so Skip/Abort can release the remaining work.
        await new Promise(resolve => setTimeout(resolve, 0));
      }
      aborted(signal);
      if (!usedFrames) fail("No chart audio is available at this preview offset.");
      onProgress("Rendering combined chart preview");
      const bytes = wavClip(clip, usedFrames);
      return {bytes, mime: "audio/wav", durationHint: usedFrames * 1000 / LIMITS.sampleRate,
        previewStartMs: 0, clip_start_ms: clipStart, previewKind: "mixed-clip"};
    } finally {
      encoded = null; decoded = null; clip = null;
      if (context?.close) {try {await context.close();} catch (_) { /* Context already released. */ }}
      context = null;
      release();
    }
  }

  async function range(url, start, length, signal, total = null) {
    aborted(signal);
    if (!Number.isSafeInteger(start) || !Number.isSafeInteger(length) || start < 0 || length < 1
        || length > LIMITS.chunkBytes || start + length > LIMITS.packageBytes) fail("The requested audio range exceeds the safety limit.");
    const end = start + length - 1;
    const controller = new AbortController();
    const onAbort = () => controller.abort();
    signal?.addEventListener("abort", onAbort, {once: true});
    let timeout = false;
    const timer = setTimeout(() => { timeout = true; controller.abort(); }, LIMITS.requestTimeoutMs);
    let reader;
    try {
      const response = await fetch(url, {method: "GET", mode: "cors", credentials: "omit",
        redirect: "error", headers: {Range: `bytes=${start}-${end}`}, signal: controller.signal});
      aborted(signal);
      if (response.status !== 206) {
        await response.body?.cancel();
        if (response.status === 404 || response.status === 410) fail("The chart audio package is no longer available.");
        if (response.status === 429) fail("The chart file service is busy. Wait before trying this preview again.");
        if (response.status === 200) fail("This chart server did not support bounded audio reads. No full package was downloaded.");
        fail(`The chart audio could not be read (HTTP ${response.status}).`);
      }
      // Content-Range is not exposed by the current CDN's CORS response. A 206
      // plus exact bounded bytes is required; verify offsets/total when readable.
      const contentRange = response.headers.get("Content-Range");
      if (contentRange) {
        const match = /^bytes (\d+)-(\d+)\/(\d+)$/.exec(contentRange);
        if (!match || Number(match[1]) !== start || Number(match[2]) !== end
            || Number(match[3]) > LIMITS.packageBytes || Number(match[3]) <= end
            || (total !== null && Number(match[3]) !== total)) fail("The chart server returned an unexpected byte range.");
      }
      const declared = response.headers.get("Content-Length");
      if (declared !== null && (!/^\d+$/.test(declared) || Number(declared) !== length)) fail("The chart server returned an unexpected audio range length.");
      if (!response.body) fail("The chart server returned no readable audio bytes.");
      reader = response.body.getReader();
      const bytes = new Uint8Array(length);
      let received = 0;
      while (true) {
        const chunk = await reader.read();
        aborted(signal);
        if (chunk.done) break;
        if (received + chunk.value.byteLength > length) fail("The chart server exceeded the bounded audio range.");
        bytes.set(chunk.value, received); received += chunk.value.byteLength;
      }
      if (received !== length) fail("The chart audio range was truncated.");
      return bytes;
    } catch (error) {
      aborted(signal);
      if (timeout) fail("The chart audio request timed out. Try the preview again.");
      throw error;
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
      if (reader) { try { await reader.cancel(); } catch (_) { /* Already closed. */ } }
      controller.abort();
    }
  }

  async function load(item, {signal, onProgress = () => {}} = {}) {
    aborted(signal);
    const hash = item?.md5;
    if (!item || item.source !== "enchor" || typeof hash !== "string" || !/^[a-f0-9]{32}$/.test(hash)) fail("This chart has no verified Enchor package identity for audio.");
    const url = `https://files.enchor.us/${hash}${item.has_video_background === true || item.hasVideoBackground === true ? "_novideo" : ""}.sng`;
    onProgress("Reading chart audio index");
    const head = await range(url, 0, 42, signal);
    if (!Array.from("SNGPKG").every((char, i) => head[i] === char.charCodeAt(0))) fail("This chart package has an unsupported audio preview format.");
    if (new DataView(head.buffer).getUint32(6, true) !== 1) fail("This chart package version is not supported for audio preview.");
    const mask = head.slice(10, 26);
    const metaLength = uint64(head, 26, "metadata section", LIMITS.metadataBytes);
    const metaCount = uint64(head, 34, "metadata count", LIMITS.metadataEntries);
    if (metaLength < 8) fail("The chart metadata section is invalid.");
    const meta = metadata(metaLength > 8 ? await range(url, 42, metaLength - 8, signal) : new Uint8Array(), metaCount);
    const indexStart = 34 + metaLength;
    const indexHead = await range(url, indexStart, 16, signal);
    const indexLength = uint64(indexHead, 0, "file index", LIMITS.indexBytes);
    const count = uint64(indexHead, 8, "file count", LIMITS.members);
    if (indexLength < 8 || count < 1) fail("The chart file index is empty or invalid.");
    const entries = index(indexLength > 8 ? await range(url, indexStart + 16, indexLength - 8, signal) : new Uint8Array(), count);
    const dataHeaderStart = indexStart + 8 + indexLength;
    const dataLength = uint64(await range(url, dataHeaderStart, 8, signal), 0, "file data");
    const dataStart = dataHeaderStart + 8, dataEnd = dataStart + dataLength;
    if (dataEnd > LIMITS.packageBytes || entries.reduce((sum, entry) => sum + entry.size, 0) !== dataLength) fail("The chart file index does not match its data section.");
    let previousEnd = dataStart;
    for (const entry of [...entries].sort((a, b) => a.offset - b.offset)) {
      if (entry.offset < dataStart || entry.offset + entry.size > dataEnd || (entry.size && entry.offset < previousEnd)) fail("The chart audio index contains overlapping or out-of-bounds files.");
      if (entry.size) previousEnd = entry.offset + entry.size;
    }
    const selected = selectAudio(entries);
    const duration = nonnegative(item.song_length_ms ?? item.song_length ?? meta.song_length);
    const previewStart = nonnegative(item.preview_start_ms ?? item.preview_start_time ?? meta.preview_start_time) || 0;
    let result;
    if (selected.single) {
      const entry = selected.entries[0];
      result = {bytes: await readAudio(entry, url, mask, dataEnd, signal, onProgress, "Loading chart audio"),
        mime: MIME[entry.name.toLowerCase().split(".").pop()], durationHint: duration,
        previewStartMs: previewStart, previewKind: "single-mix"};
    } else {
      result = await mixedClip(selected.entries, url, mask, dataEnd, duration, previewStart, signal, onProgress);
    }
    aborted(signal);
    onProgress("Chart audio ready");
    aborted(signal);
    const objectUrl = URL.createObjectURL(new Blob([result.bytes], {type: result.mime}));
    let cleaned = false;
    const cleanup = () => { if (!cleaned) { cleaned = true; URL.revokeObjectURL(objectUrl); signal?.removeEventListener("abort", cleanup); } };
    signal?.addEventListener("abort", cleanup, {once: true});
    const {bytes: _bytes, ...properties} = result;
    return {url: objectUrl, ...properties, cleanup};
  }
  window.RiffCatalogAudio = Object.freeze({load, limits: LIMITS});
})();
