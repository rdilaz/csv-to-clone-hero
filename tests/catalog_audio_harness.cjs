/* Offline harness: every provider response comes from the supplied synthetic SNG. */
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const bytes = Buffer.from(input.package || '', 'base64');
const requests = [];
const controller = new AbortController();
let created = 0, revoked = 0;
let decodes = 0, contextsClosed = 0;
const create = URL.createObjectURL.bind(URL), revoke = URL.revokeObjectURL.bind(URL);
URL.createObjectURL = blob => { created++; return create(blob); };
URL.revokeObjectURL = url => { revoked++; return revoke(url); };
global.window = global;
if (input.decoded) global.OfflineAudioContext = class {
  async decodeAudioData() {
    const shape = input.decoded[Math.min(decodes, input.decoded.length - 1)]; decodes++;
    if (input.abortDuringDecode) controller.abort();
    if (input.decodeError) throw new Error('synthetic codec rejection');
    return {length:shape.frames, sampleRate:shape.sampleRate || 22050, numberOfChannels:shape.channels || 1,
      getChannelData: () => new Float32Array(shape.frames).fill(shape.value || 0)};
  }
  async close() {contextsClosed++;}
};
global.fetch = async (url, options) => {
  const match = /^bytes=(\d+)-(\d+)$/.exec(options.headers.Range);
  requests.push({url, range: options.headers.Range, credentials: options.credentials, redirect: options.redirect});
  if (input.abortAt === requests.length) controller.abort();
  if (options.signal.aborted) throw new DOMException('cancelled', 'AbortError');
  const start = Number(match[1]), end = Number(match[2]);
  let body = bytes.subarray(start, end + 1);
  if (input.truncateAt === requests.length) body = body.subarray(0, Math.max(0, body.length - 1));
  if (input.overrunAt === requests.length) body = Buffer.concat([body, Buffer.from([1])]);
  const status = input.status || 206;
  const headers = {'Content-Length': String(body.length)};
  if (!input.hiddenContentRange) headers['Content-Range'] = input.badRange ? `bytes 1-${end}/${bytes.length}` : `bytes ${start}-${end}/${bytes.length}`;
  if (input.omitContentLength) delete headers['Content-Length'];
  return new Response(body, {status, headers});
};
vm.runInThisContext(fs.readFileSync(process.argv[2], 'utf8'));
(async () => {
  try {
    const progress = [];
    const item = Object.assign({source: 'enchor', md5: 'a'.repeat(32), song_length_ms: 1000}, input.item || {});
    const resource = await RiffCatalogAudio.load(item, {signal: controller.signal,
      onProgress: text => { progress.push(text); if (input.abortAtReady && text === 'Chart audio ready') controller.abort(); }});
    const contents = Buffer.from(await (await fetchBlob(resource.url)).arrayBuffer());
    resource.cleanup(); resource.cleanup();
    process.stdout.write(JSON.stringify({ok: true, mime: resource.mime, durationHint: resource.durationHint,
      previewStartMs: resource.previewStartMs, clip_start_ms:resource.clip_start_ms, previewKind:resource.previewKind,
      audio: contents.toString('base64'), progress, requests, created, revoked, decodes, contextsClosed}));
  } catch (error) {
    process.stdout.write(JSON.stringify({ok: false, name: error.name, error: error.message, requests, created, revoked, decodes, contextsClosed}));
  }
})();
async function fetchBlob(url) {
  // Native Blob fetch only; the provider fetch above is an entirely separate mock.
  const {resolveObjectURL} = require('node:buffer');
  return {arrayBuffer: () => resolveObjectURL(url).arrayBuffer()};
}
