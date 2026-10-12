"""Real browser media decoding from tiny synthetic WAV-in-SNG responses only."""
from __future__ import annotations

import json
import re

import pytest

from .catalog_audio_fixtures import constant_wav, sng_audio
from .test_public_browser import playwright, public_server
from .test_public_delivery_browser import delivery_chromium

pytestmark = pytest.mark.browser


@pytest.fixture
def audio_page(public_server, delivery_chromium):
    _app, base_url, _name, _cookie = public_server
    context = delivery_chromium.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    page.goto(base_url + "/", wait_until="domcontentloaded")
    page.add_script_tag(url=base_url + "/static/catalog-audio.js")
    yield page
    context.close()


def _ranges(page, package, *, delayed_first=False):
    requests = []

    def respond(route):
        header = route.request.headers.get("range", "")
        match = re.fullmatch(r"bytes=(\d+)-(\d+)", header)
        assert match, header
        start, end = map(int, match.groups())
        requests.append((start, end))
        # Intentionally omit Content-Range exposure like the real CDN probe.
        route.fulfill(status=206, headers={"Access-Control-Allow-Origin": "*",
            "Content-Type": "application/octet-stream"}, body=package[start:end + 1])

    page.route("https://files.enchor.us/**", respond)
    return requests


def test_native_media_decodes_real_wav_after_bounded_container_reads(audio_page):
    page = audio_page
    package = sng_audio()
    requests = _ranges(page, package)
    result = page.evaluate("""async () => {
      const resource = await RiffCatalogAudio.load({source:'enchor',md5:'a'.repeat(32), song_length_ms:250});
      const audio = document.createElement('audio');
      audio.controls = true; document.body.append(audio);
      const loaded = new Promise((resolve,reject) => {
        audio.addEventListener('loadedmetadata', () => resolve(audio.duration), {once:true});
        audio.addEventListener('error', () => reject(new Error('Native decoder rejected fixture WAV')), {once:true});
      });
      audio.src = resource.url;
      const duration = await loaded;
      const playing = new Promise(resolve => audio.addEventListener('playing', () => resolve(true), {once:true}));
      await audio.play(); await playing;
      const actualPlaying = !audio.paused;
      audio.pause(); audio.removeAttribute('src'); audio.load(); resource.cleanup();
      return {duration, actualPlaying, previewStartMs:resource.previewStartMs};
    }""")
    assert result["actualPlaying"] is True
    assert result["duration"] == pytest.approx(0.25)
    assert result["previewStartMs"] == 25
    assert len(requests) == 6
    assert (0, len(package) - 1) not in requests
    assert sum(end - start + 1 for start, end in requests) < len(package)


def test_browser_abort_before_start_never_requests_provider(audio_page):
    page = audio_page
    requests = _ranges(page, sng_audio())
    result = page.evaluate("""async () => {
      const controller = new AbortController(); controller.abort();
      try {await RiffCatalogAudio.load({source:'enchor',md5:'a'.repeat(32)}, {signal:controller.signal});return 'unexpected';}
      catch(error) {return error.name;}
    }""")
    assert result == "AbortError" and requests == []


def test_browser_no_range_support_cancels_without_full_package_fallback(audio_page):
    page = audio_page
    calls = []
    page.route("https://files.enchor.us/**", lambda route: (calls.append(route.request.url), route.fulfill(
        status=200, headers={"Access-Control-Allow-Origin": "*"}, body=b"range deliberately unsupported")))
    result = page.evaluate("""async () => {
      try {await RiffCatalogAudio.load({source:'enchor',md5:'a'.repeat(32)});return 'unexpected';}
      catch(error) {return error.message;}
    }""")
    assert "bounded audio reads" in result and len(calls) == 1


def test_browser_cancels_stale_member_before_creating_any_blob(audio_page):
    page = audio_page
    requests = _ranges(page, sng_audio())
    result = page.evaluate("""async () => {
      const controller = new AbortController();
      let created = 0;
      const original = URL.createObjectURL.bind(URL);
      URL.createObjectURL = blob => {created++;return original(blob);};
      try {
        await RiffCatalogAudio.load({source:'enchor',md5:'a'.repeat(32)}, {signal:controller.signal,
          onProgress:text => {if(text.startsWith('Loading chart audio')) controller.abort();}});
        return {unexpected:true};
      } catch(error) {return {name:error.name,created};}
      finally {URL.createObjectURL=original;}
    }""")
    assert result == {"name": "AbortError", "created": 0}
    assert len(requests) == 5


def _mix_browser(page, *, duration_ms, preview_start_ms=0):
    return page.evaluate("""async ({duration,offset}) => {
      const create = URL.createObjectURL.bind(URL);
      let produced;
      URL.createObjectURL = blob => {produced=blob;return create(blob);};
      let resource;
      try {
        resource = await RiffCatalogAudio.load({source:'enchor',md5:'a'.repeat(32),
          song_length_ms:duration,preview_start_ms:offset});
        const bytes = await produced.arrayBuffer(), view = new DataView(bytes);
        const audio = document.createElement('audio'); document.body.append(audio);
        const loaded = new Promise((resolve,reject) => {
          audio.addEventListener('loadedmetadata',()=>resolve(audio.duration),{once:true});
          audio.addEventListener('error',()=>reject(new Error('Mixed WAV did not decode')),{once:true});
        });
        audio.src=resource.url;
        const nativeDuration=await loaded;
        audio.removeAttribute('src');audio.load();audio.remove();
        return {kind:resource.previewKind,seek:resource.previewStartMs,start:resource.clip_start_ms,
          durationHint:resource.durationHint,nativeDuration,size:bytes.byteLength,
          sampleRate:view.getUint32(24,true),channels:view.getUint16(22,true),
          left:view.getInt16(44,true),right:view.getInt16(46,true)};
      } finally {resource?.cleanup();URL.createObjectURL=create;}
    }""", {"duration": duration_ms, "offset": preview_start_ms})


def test_native_two_stem_mix_aligns_source_offset_and_mono_channels(audio_page):
    page = audio_page
    package = sng_audio([
        ("song.wav", constant_wav([(0.1, 1000), (0.2, 8192)])),
        ("guitar.wav", constant_wav([(0.3, 8192)])),
    ])
    requests = _ranges(page, package)
    result = _mix_browser(page, duration_ms=300, preview_start_ms=100)
    assert result["kind"] == "mixed-clip" and result["seek"] == 0 and result["start"] == 100
    assert result["durationHint"] == pytest.approx(200, abs=0.1)
    assert result["nativeDuration"] == pytest.approx(0.2, abs=0.001)
    assert result["sampleRate"] == 22050 and result["channels"] == 2
    assert result["left"] == result["right"] == pytest.approx(11585, abs=3)
    assert len(requests) == 7  # Five container reads, then both complete stems.


@pytest.mark.parametrize("amplitude,expected", [(32767, 32767), (-32768, -32768)])
def test_native_mixer_clamps_both_peak_polarities_without_integer_wrap(audio_page, amplitude, expected):
    page = audio_page
    _ranges(page, sng_audio([("song.wav", constant_wav([(0.1, amplitude)])),
        ("bass.wav", constant_wav([(0.1, amplitude)]))]))
    result = _mix_browser(page, duration_ms=100)
    assert result["left"] == result["right"] == expected


def test_native_stem_render_never_outputs_more_than_45_seconds(audio_page):
    page = audio_page
    source = constant_wav([(50, 4096)])
    _ranges(page, sng_audio([("song.wav", source), ("drums.wav", source)]))
    result = _mix_browser(page, duration_ms=50000)
    assert result["durationHint"] == 45000
    assert result["nativeDuration"] == pytest.approx(45)
    assert result["size"] == 44 + 45 * 22050 * 2 * 2


def test_browser_skip_discards_first_decoded_stem_without_fetching_second(audio_page):
    page = audio_page
    requests = _ranges(page, sng_audio([("song.wav", constant_wav([(0.2, 4096)])),
        ("drums.wav", constant_wav([(0.2, 8192)]))]))
    result = page.evaluate("""async () => {
      const controller=new AbortController();let created=0;
      const create=URL.createObjectURL.bind(URL);
      URL.createObjectURL=blob=>{created++;return create(blob);};
      try {
        await RiffCatalogAudio.load({source:'enchor',md5:'a'.repeat(32),song_length_ms:200},
          {signal:controller.signal,onProgress:message=>{if(message.startsWith('Loading chart stem 2'))controller.abort();}});
        return {unexpected:true};
      } catch(error){return {name:error.name,created};}
      finally{URL.createObjectURL=create;}
    }""")
    assert result == {"name": "AbortError", "created": 0}
    assert len(requests) == 6
