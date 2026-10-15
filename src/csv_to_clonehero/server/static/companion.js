/* Local companion, sharing catalog.js request/format helpers, catalog-audio.js
 * bounded SNG reader, and riff-scene.js opt-in playback-driven vinyl scene.
 * Audio cancellation and object URL cleanup follow the existing catalog player. */
(() => {
  "use strict";
  const $ = selector => document.querySelector(selector);
  const root = $("#companion"), audio = $("#companionAudio");
  if (!root || !audio) return;
  const {api, count, time, newSeed} = window.RiffCatalogShared;
  const state = {items: [], current: null, total: 0, filtered: 0, offset: 0, hasMore: false,
    q: "", decade: "", seed: "", cachedOnly: true, pageEpoch: 0, pageController: null, pageLoading: false,
    loadEpoch: 0, controller: null, resource: null, loadedKey: null, playback: "paused",
    history: [], historyIndex: -1, status: null, installItem: null, installing: false, swapTimer: null, inventoryTimer: null};
  const validKey = key => /^enchor:[1-9]\d*$/.test(key || "");
  const message = text => { $("#playerStatus").textContent = text; $("#playerStatus").title = text; };
  const localAudio = item => item?.preview?.available !== false && /^\/api\/companion\/audio\/[a-f0-9]{32}\.sng$/.test(item?.preview?.local_url || "");
  const svg = content => { const node = document.createElementNS("http://www.w3.org/2000/svg", "svg"); node.setAttribute("viewBox", "0 0 24 24"); node.innerHTML = content; return node; };

  function playback(value) {
    state.playback = value;
    root.dataset.playback = $("#deck").dataset.playback = value;
    $("#record").dataset.playback = value;
    $("#playPause").dataset.state = value;
    const loading = value === "loading", playing = value === "playing";
    $("#playPause").setAttribute("aria-label", loading ? "Cancel preview loading" : playing ? "Pause preview" : "Play preview");
    $("#playPause").title = loading ? "Cancel preview loading" : playing ? "Pause preview" : "Play preview";
    $("#playIcon").innerHTML = playing ? '<path d="M7 5h4v14H7zm7 0h4v14h-4Z"/>' : '<path d="m9 5 11 7-11 7Z"/>';
    $(".loading-ring").hidden = !loading;
    $("#deckState").textContent = loading ? "OPENING THE RECORD" : playing ? "NOW SPINNING" : value === "buffering" ? "BUFFERING AUDIO" : "READY WHEN YOU ARE";
    document.dispatchEvent(new CustomEvent("riff:companion-playback", {detail: {state: value}}));
  }
  function stopMedia() {
    ++state.loadEpoch;
    state.controller?.abort(); state.controller = null;
    audio.pause(); audio.removeAttribute("src"); audio.load();
    state.resource?.cleanup?.(); state.resource = null; state.loadedKey = null;
    playback("paused"); updateTime();
  }
  function updateTime() {
    const duration = Number.isFinite(audio.duration) ? audio.duration : 0;
    const seek = $("#seek"); seek.disabled = !duration; seek.max = String(duration || 100); seek.value = String(audio.currentTime || 0);
    $("#elapsed").textContent = time(audio.currentTime || 0); $("#duration").textContent = duration ? time(duration) : "—:—";
  }
  function currentMetadata(item) {
    $("#nowTitle").textContent = item?.title || "Find your next favorite.";
    $("#nowTitle").title = item?.title || "";
    $("#nowArtist").textContent = item ? [item.artist || "Unknown artist", item.album, item.parsed_year || item.year].filter(Boolean).join(" · ") : "A little room for a lot of music.";
    $("#labelTitle").textContent = item?.title || "YOUR NEXT GREAT FIND";
    $("#labelArtist").textContent = item?.artist || "PICK A RECORD";
    $("#nowBadge").textContent = item?.installed ? "IN LIBRARY" : localAudio(item) ? "CACHED AUDIO" : "NO CACHED AUDIO";
    $("#nowBadge").title = item?.installed ? "A matching song is in the .sng inventory; the exact chart version may differ." : "Local package audio is checked when opened.";
    $("#record").dataset.catalogKey = item?.catalog_key || "";
    $("#record").setAttribute("data-testid", "current-record");
    const art = item?.artwork_url || item?.art_url || item?.artwork?.url;
    const image = $("#recordArt");
    const safeArt = typeof art === "string" && /^\/api\/companion\/(?:art|artwork)\//.test(art);
    image.hidden = !safeArt;
    if (safeArt) image.src = art; else image.removeAttribute("src");
    $("#playPause").disabled = !item;
    $("#previous").disabled = state.historyIndex <= 0;
    $("#next").disabled = !state.items.length && !state.hasMore;
  }
  function select(item, {remember = true} = {}) {
    if (!item || !validKey(item.catalog_key)) return;
    const changed = state.current?.catalog_key !== item.catalog_key;
    if (changed) {
      stopMedia();
      const wrap = $("#recordWrap"); wrap.classList.remove("swapping"); void wrap.offsetWidth; wrap.classList.add("swapping");
      clearTimeout(state.swapTimer); state.swapTimer = setTimeout(() => wrap.classList.remove("swapping"), 500);
      if (remember) { state.history.splice(state.historyIndex + 1); state.history.push(item); if (state.history.length > 100) state.history.shift(); state.historyIndex = state.history.length - 1; }
    }
    state.current = item;
    currentMetadata(item); renderRows();
    message(localAudio(item) ? "Cached chart audio · press Play to check this preview." : item.preview?.reason || "No cached audio for this chart. Choose another record to listen.");
  }
  async function play(item = state.current) {
    if (!item) { if (state.items.length) select(state.items[0]); return; }
    if (state.current?.catalog_key !== item.catalog_key) select(item);
    if (!localAudio(item)) { message(item.preview?.reason || "This record has no cached preview. Try another chart."); return; }
    if (state.loadedKey === item.catalog_key && audio.getAttribute("src")) {
      if (audio.ended) audio.currentTime = 0;
      try { await audio.play(); } catch (_) { message("Your browser paused playback. Press Play to try again."); } return;
    }
    stopMedia();
    const epoch = ++state.loadEpoch, controller = new AbortController(); state.controller = controller; playback("loading"); message("Reading the cached chart's audio index…");
    try {
      const resource = await window.RiffCatalogAudio.load(item, {signal: controller.signal, localUrl: item.preview.local_url,
        onProgress: text => { if (epoch === state.loadEpoch) message(String(text)); }});
      if (epoch !== state.loadEpoch || state.current?.catalog_key !== item.catalog_key) { resource.cleanup?.(); return; }
      if (!/^blob:/.test(resource.url || "")) { resource.cleanup?.(); throw new Error("This preview could not be safely opened."); }
      state.resource = resource; state.loadedKey = item.catalog_key;
      audio.addEventListener("loadedmetadata", () => {
        if (epoch !== state.loadEpoch) return;
        const start = Math.max(0, Number(resource.previewStartMs || 0) / 1000);
        if (start && Number.isFinite(audio.duration)) audio.currentTime = Math.min(start, Math.max(0, audio.duration - .1));
        updateTime();
      }, {once: true});
      audio.src = resource.url; audio.load(); await audio.play();
    } catch (error) {
      if (epoch !== state.loadEpoch || error.name === "AbortError") return;
      stopMedia(); message(error.name === "NotAllowedError" ? "Your browser paused playback. Press Play to try again." : error.message || "This chart's audio could not be opened.");
    } finally { if (epoch === state.loadEpoch) state.controller = null; }
  }
  async function toggle() {
    if (state.playback === "loading") { stopMedia(); message("Preview loading cancelled."); return; }
    if (!audio.paused) { audio.pause(); return; }
    await play();
  }
  async function next() {
    const wasPlaying = !audio.paused || state.playback === "loading";
    if (state.historyIndex + 1 < state.history.length) { state.historyIndex++; select(state.history[state.historyIndex], {remember: false}); }
    else {
      let index = state.items.findIndex(item => item.catalog_key === state.current?.catalog_key) + 1;
      if (index >= state.items.length && state.hasMore) { await browse(true); index = state.items.findIndex(item => item.catalog_key === state.current?.catalog_key) + 1; }
      if (!state.items.length) return;
      select(state.items[index % state.items.length]);
    }
    if (wasPlaying) await play();
  }
  function renderRows() {
    const host = $("#results");
    const focus = document.activeElement?.dataset?.focusKey;
    host.replaceChildren();
    if (!state.items.length) {
      const empty = document.createElement("p"); empty.className = "empty-state"; empty.textContent = state.pageLoading ? "Taking the records off the shelf…" : state.cachedOnly ? "No cached records match. Turn off Cached packages to search the whole catalog." : "No records found. Try another title, artist, album or decade."; host.append(empty);
    }
    state.items.forEach((item, index) => {
      const row = document.createElement("article"); row.className = "song-row"; row.dataset.catalogKey = item.catalog_key; row.dataset.current = String(item.catalog_key === state.current?.catalog_key);
      const number = document.createElement("span"); number.className = "song-number"; number.textContent = String(index + 1).padStart(2, "0"); number.setAttribute("aria-hidden", "true");
      const info = document.createElement("button"); info.type = "button"; info.className = "song-info"; info.dataset.focusKey = `${item.catalog_key}:select`; info.setAttribute("aria-label", `Select ${item.title} by ${item.artist}`);
      const title = document.createElement("strong"); title.textContent = item.title || "Untitled";
      const detail = document.createElement("span"); detail.textContent = [item.artist || "Unknown artist", item.album, item.parsed_year || item.year].filter(Boolean).join(" · "); info.title = [item.title, detail.textContent].join(" — "); info.append(title, detail); info.addEventListener("click", () => select(item));
      row.append(number, info);
      if (item.installed) { const badge = document.createElement("span"); badge.className = "song-badge"; badge.textContent = "In library"; badge.title = "Matching artist/title found in .sng inventory; exact chart version may differ."; row.append(badge); }
      const listen = document.createElement("button"); listen.type = "button"; listen.className = "song-play"; listen.dataset.focusKey = `${item.catalog_key}:play`; listen.setAttribute("aria-label", `Play preview of ${item.title}`); listen.title = localAudio(item) ? "Play cached preview" : item.preview?.reason || "No cached preview"; listen.disabled = !localAudio(item); listen.append(svg('<path class="fill" d="m8 4 13 8-13 8Z"/>')); listen.addEventListener("click", () => void play(item));
      const add = document.createElement("button"); add.type = "button"; add.className = "song-add"; add.dataset.focusKey = `${item.catalog_key}:add`; add.setAttribute("aria-label", `Add ${item.title} package to sandbox folder`); add.title = item.install_available ? "Add cached package to sandbox" : item.install_reason || "No cached installable package"; add.disabled = item.install_available !== true; add.append(svg('<path d="M12 5v14M5 12h14"/>')); add.addEventListener("click", () => openInstall(item));
      row.append(listen, add); host.append(row);
    });
    if (focus) [...host.querySelectorAll("[data-focus-key]")].find(node => node.dataset.focusKey === focus)?.focus({preventScroll: true});
    $("#loadMore").hidden = !state.hasMore; $("#loadMore").disabled = state.pageLoading;
    $("#resultsSummary").textContent = `${count(state.filtered)} ${state.cachedOnly ? "cached " : state.q || state.decade ? "matching " : ""}charts · ${count(state.items.length)} shown`;
  }
  async function browse(append = false) {
    state.pageController?.abort(); const controller = new AbortController(); state.pageController = controller;
    const epoch = ++state.pageEpoch; state.pageLoading = true; $("#results").setAttribute("aria-busy", "true");
    const offset = append ? state.items.length : 0;
    const params = new URLSearchParams({q: state.q, decade: state.decade, offset: String(offset), limit: "40", shuffle_seed: state.seed, cached_only: state.cachedOnly ? "1" : "0"});
    try {
      const data = await api(`/api/companion/catalog?${params}`, {signal: controller.signal});
      if (epoch !== state.pageEpoch) return;
      state.items = append ? state.items.concat(data.items || []) : data.items || [];
      state.total = Number(data.total_charts ?? data.total ?? 0); state.filtered = Number(data.total_filtered_charts ?? data.filtered ?? state.total); state.offset = offset; state.hasMore = data.has_more === true;
      $("#catalogCount").textContent = `${count(state.total)} charts${Number.isFinite(data.total_songs) ? ` · ${count(data.total_songs)} songs` : ""}`;
      if (!state.current && state.items.length) select(state.items.find(localAudio) || state.items[0]);
      renderRows(); currentMetadata(state.current);
    } catch (error) {
      if (epoch !== state.pageEpoch || error.name === "AbortError") return;
      $("#catalogCount").textContent = "Catalog unavailable"; $("#results").replaceChildren();
      const note = document.createElement("p"); note.className = "empty-state"; note.textContent = error.message || "The local catalog could not be opened.";
      const retry = document.createElement("button"); retry.className = "text-button"; retry.textContent = "Try again ↗"; retry.addEventListener("click", () => void browse()); $("#results").append(note, retry);
    } finally { if (epoch === state.pageEpoch) { state.pageLoading = false; state.pageController = null; $("#results").setAttribute("aria-busy", "false"); $("#loadMore").disabled = false; } }
  }
  function showDialog(id) { const dialog = $(id); if (!dialog.open) dialog.showModal(); }
  async function refreshStatus() {
    try {
      state.status = await api("/api/companion/status");
      const library = state.status.library || {};
      const inventoryPending = library.status === "scanning" || library.installed_count === null;
      $("#installedCount").textContent = inventoryPending ? "Scanning library…" : `${count(library.installed_count)} in library`;
      if (document.activeElement !== $("#folderPath")) $("#folderPath").value = state.status.folder || state.status.sandbox_root || "";
      $("#sandboxHint").textContent = `Sandbox: ${state.status.sandbox_root || "not available"}. Choose an existing folder inside this location.`;
      $("#libraryPath").textContent = library.path || (library.roots || []).join("\n") || "No library linked.";
      $("#libraryDetail").textContent = inventoryPending ? "Scanning .sng packages read-only. Installed badges appear after the scan finishes. Unpacked song folders are not indexed here."
        : `${count(library.scanned_count ?? library.installed_count)} .sng packages indexed. Badges match song metadata, not exact chart versions. Unpacked song folders are not indexed here.`;
      clearTimeout(state.inventoryTimer);
      if (inventoryPending) state.inventoryTimer = setTimeout(async () => { await refreshStatus(); if (state.status?.library?.status !== "scanning" && state.status?.library?.installed_count !== null) await browse(); }, 1800);
    } catch (error) { $("#folderMessage").textContent = error.message || "Local folder status is unavailable."; }
  }
  function openFolder() { $("#folderMessage").textContent = ""; showDialog("#folderDialog"); }
  function openInstall(item) {
    state.installItem = item; $("#installTitle").textContent = "Add this chart?";
    $("#installSong").textContent = `${item.title} · ${item.artist}${item.charter ? ` · Chart by ${item.charter}` : ""}`;
    $("#installDestination").textContent = state.status?.folder || "Choose a sandbox destination first.";
    $("#installReason").textContent = !state.status?.folder ? "Select a sandbox folder before adding this package." : item.install_available ? "One add-only operation. Existing files are preserved." : item.install_reason || "This package is not available locally.";
    $("#installReason").dataset.error = "false";
    $("#confirmInstall").disabled = !state.status?.folder || item.install_available !== true || state.installing;
    showDialog("#installDialog");
  }
  $("#playPause").addEventListener("click", () => void toggle());
  $("#next").addEventListener("click", () => void next());
  $("#previous").addEventListener("click", () => { if (state.historyIndex <= 0) return; const wasPlaying = !audio.paused; state.historyIndex--; select(state.history[state.historyIndex], {remember: false}); if (wasPlaying) void play(); });
  $("#mute").addEventListener("click", () => { audio.muted = !audio.muted; $("#mute").setAttribute("aria-pressed", String(audio.muted)); $("#mute").setAttribute("aria-label", audio.muted ? "Unmute preview" : "Mute preview"); $("#volumeWaves").setAttribute("d", audio.muted ? "m17 9 5 6m0-6-5 6" : "M16 8c2 2 2 6 0 8m3-11c4 4 4 10 0 14"); });
  $("#seek").addEventListener("input", event => { if (Number.isFinite(audio.duration)) audio.currentTime = Math.max(0, Math.min(Number(event.target.value), audio.duration)); });
  $("#shuffle").addEventListener("click", () => { state.seed = state.seed ? "" : newSeed(); $("#shuffle").setAttribute("aria-pressed", String(Boolean(state.seed))); void browse(); });
  let searchTimer;
  function search() { clearTimeout(searchTimer); state.q = $("#search").value.trim().slice(0, 200); state.decade = $("#decade").value; void browse(); }
  $("#searchForm").addEventListener("submit", event => { event.preventDefault(); search(); });
  $("#search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(search, 250); });
  $("#decade").addEventListener("change", search);
  $("#cachedToggle").addEventListener("click", () => { state.cachedOnly = !state.cachedOnly; $("#cachedToggle").setAttribute("aria-pressed", String(state.cachedOnly)); void browse(); });
  $("#loadMore").addEventListener("click", () => void browse(true));
  $("#openFolder").addEventListener("click", openFolder); $("#libraryInfo").addEventListener("click", openFolder);
  $("#aboutButton").addEventListener("click", () => showDialog("#aboutDialog"));
  $("#changeFolder").addEventListener("click", () => { $("#installDialog").close(); openFolder(); });
  $("#saveFolder").addEventListener("click", async () => {
    const button = $("#saveFolder"); button.disabled = true; $("#folderMessage").dataset.error = "false"; $("#folderMessage").textContent = "Checking the sandbox folder…";
    try { const result = await api("/api/companion/folder", {method: "POST", body: {path: $("#folderPath").value.trim()}}); await refreshStatus(); $("#folderMessage").textContent = `Folder selected. ${result.message || "Choose a chart's + button to review a specific add."}`; }
    catch (error) { $("#folderMessage").dataset.error = "true"; $("#folderMessage").textContent = error.message; }
    finally { button.disabled = false; }
  });
  $("#confirmInstall").addEventListener("click", async () => {
    if (state.installing || !state.installItem || !state.status?.folder) return;
    state.installing = true; $("#confirmInstall").disabled = true; $("#installReason").dataset.error = "false"; $("#installReason").textContent = "Adding this cached package…";
    try {
      const result = await api("/api/companion/install", {method: "POST", body: {catalog_key: state.installItem.catalog_key, destination: state.status.folder, confirm: true}});
      $("#installTitle").textContent = "Record added."; $("#installReason").textContent = result.message || "Package added to the sandbox. Existing files were preserved.";
      await refreshStatus(); await browse();
    } catch (error) { $("#installReason").dataset.error = "true"; $("#installReason").textContent = `${error.message || "The add could not be confirmed."} No automatic retry was sent.`; }
    finally { state.installing = false; }
  });
  for (const dialog of document.querySelectorAll("dialog")) dialog.addEventListener("click", event => { if (event.target === dialog) { const rect = dialog.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close(); } });
  audio.addEventListener("playing", () => { playback("playing"); message(state.resource?.previewKind === "mixed-clip" ? "Playing a mixed stem preview · up to 45 seconds." : "Playing cached chart audio · availability confirmed for this preview."); });
  audio.addEventListener("pause", () => { if (state.playback !== "loading") playback("paused"); });
  audio.addEventListener("waiting", () => { if (audio.getAttribute("src")) playback("buffering"); });
  audio.addEventListener("timeupdate", updateTime); audio.addEventListener("durationchange", updateTime);
  audio.addEventListener("ended", () => { playback("paused"); message("Preview finished. Choose another record or press Next."); });
  audio.addEventListener("error", () => { if (audio.getAttribute("src")) { playback("paused"); message("This browser could not decode the cached chart audio. Try another record."); } });
  $("#recordArt").addEventListener("error", event => { event.target.hidden = true; });
  window.addEventListener("pagehide", () => { clearTimeout(state.inventoryTimer); stopMedia(); });
  playback("paused");
  (async () => {
    await Promise.allSettled([refreshStatus(), browse()]);
    const key = new URLSearchParams(location.search).get("song");
    if (validKey(key)) { try { const [source, id] = key.split(":"); const data = await api(`/api/companion/catalog/${source}/${id}`); if (data.item?.catalog_key === key) select(data.item); } catch (error) { message(error.message); } }
  })();
})();
