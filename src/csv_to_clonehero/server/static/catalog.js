(() => {
  "use strict";
  const $ = selector => document.querySelector(selector);
  // Shared with the local companion; the public workbench keeps its own state.
  const count = value => Number(value || 0).toLocaleString();
  const time = value => Number.isFinite(value) ? `${Math.floor(Math.max(0, value) / 60)}:${String(Math.floor(Math.max(0, value) % 60)).padStart(2, "0")}` : "-";
  const newSeed = () => { const bytes = new Uint8Array(8); crypto.getRandomValues(bytes); return [...bytes].map(value => value.toString(16).padStart(2, "0")).join(""); };
  window.RiffCatalogShared = Object.freeze({api, count, time, newSeed});
  const root = $("#catalogWorkbench"), audio = $("#catalogAudio");
  if (!root || !audio) return;
  const LIMIT = 50, QUEUE_LIMIT = 200, CONTINUATION_BATCH = 10;
  const storageKey = `riff:catalog-queue:1:${document.body.dataset.riffOwner || "guest"}`;
  const state = {items: [], queue: [], index: -1, offset: 0, total: 0, filtered: 0,
    snapshot: null, q: "", decade: "", seed: "", loading: false, request: 0, controller: null,
    tab: "catalog", opener: null, busy: false, receipt: null, current: null,
    loadEpoch: 0, loadController: null, resource: null, loadedKey: null,
    playback: "paused", previewError: false, autoFailures: 0, sourcePage: location.pathname, pageRequest: 0,
    basePath: location.pathname, baseTitle: document.title, choiceChecks: new Map(), choiceRevisions: new Map(),
    receiptPromise: null, receiptNotify: false, awaitingSelection: false,
    continueCatalog: true, continuationActive: false, feed: null, feedLoading: false, lookupKey: null,
    recordKey: null, recordPlayback: null, swapTimer: null};
  const validKey = key => /^enchor:[1-9]\d*$/.test(key || "");
  const artifactKey = item => /^[a-f0-9]{32}$/i.test(item?.md5 || "") && item?.source
    ? `${item.source}:${item.md5.toLowerCase()}` : null;
  const message = (text, node = "#catalogPlayerStatus") => { $(node).textContent = text; };
  function save() {
    try { sessionStorage.setItem(storageKey, JSON.stringify({version: 1, index: state.index, continueCatalog: state.continueCatalog,
      muted: audio.muted, volume: audio.volume,
      feed: state.feed && {q: state.feed.q, decade: state.feed.decade, seed: state.feed.seed, offset: state.feed.offset,
        snapshot: state.feed.snapshot, exhausted: state.feed.exhausted},
      queue: state.queue.slice(0, QUEUE_LIMIT).map(item => ({catalog_key: item.catalog_key,
        title: String(item.title || "").slice(0, 240), artist: String(item.artist || "").slice(0, 240),
        charter: String(item.charter || "").slice(0, 120), automatic: item.automatic === true}))})); } catch (_) { /* Current session still works. */ }
  }
  function restore() {
    try {
      const stored = JSON.parse(sessionStorage.getItem(storageKey) || "null");
      if (stored?.version !== 1 || !Array.isArray(stored.queue)) return;
      state.queue = stored.queue.slice(0, QUEUE_LIMIT).filter(item => validKey(item?.catalog_key))
        .map(item => ({catalog_key: item.catalog_key, title: String(item.title || "Chart").slice(0, 240),
          artist: String(item.artist || "").slice(0, 240), charter: String(item.charter || "").slice(0, 120), automatic: item.automatic === true}));
      state.continueCatalog = stored.continueCatalog !== false;
      audio.muted = stored.muted === true;
      if (typeof stored.volume === "number" && Number.isFinite(stored.volume) && stored.volume >= 0 && stored.volume <= 1) audio.volume = stored.volume;
      $("#catalogVolume").value = String(audio.volume);
      if (stored.feed && Number.isSafeInteger(stored.feed.offset) && stored.feed.offset >= 0) {
        state.feed = {q: String(stored.feed.q || "").slice(0, 200), decade: /^(19\d0|20[0-2]0)$/.test(stored.feed.decade || "") ? stored.feed.decade : "", seed: String(stored.feed.seed || "").slice(0, 64),
          offset: stored.feed.offset, snapshot: typeof stored.feed.snapshot === "string" && stored.feed.snapshot.length <= 4096 ? stored.feed.snapshot : null,
          exhausted: stored.feed.exhausted === true};
      }
      state.index = Number.isInteger(stored.index) ? Math.min(Math.max(stored.index, -1), state.queue.length - 1) : -1;
      state.current = state.queue[state.index] || null;
      if (state.current) message("Listening queue restored. Press Play to check this chart's audio.");
    } catch (_) { /* Ignore malformed optional browser storage. */ }
  }
  async function api(path, options = {}) {
    const controller = new AbortController();
    const abort = () => controller.abort();
    options.signal?.addEventListener("abort", abort, {once: true});
    if (options.signal?.aborted) controller.abort();
    const timer = setTimeout(abort, options.method === "POST" ? 15000 : 10000);
    try {
      const response = await fetch(path, {credentials: "same-origin", method: options.method || "GET",
        headers: {"Content-Type": "application/json", "X-Riff-Request": "1"}, body: options.body ? JSON.stringify(options.body) : undefined,
        signal: controller.signal});
      const data = await response.json();
      if (!response.ok) {
        const error = new Error(data.error || data.readiness?.message || "This request could not be confirmed.");
        error.status = response.status; error.data = data; throw error;
      }
      return data;
    } catch (error) {
      if (error.name === "AbortError" && !options.signal?.aborted) throw new Error("The request timed out. Check status before another change.");
      throw error;
    } finally { clearTimeout(timer); options.signal?.removeEventListener("abort", abort); }
  }
  function applyReceipt(receipt) {
    if (!Number.isSafeInteger(receipt?.selection_revision)) return;
    if (state.receipt && receipt.selection_revision < state.receipt.selection_revision) return;
    const previous = state.receipt;
    const sameRevision = previous?.selection_revision === receipt.selection_revision;
    state.receipt = sameRevision ? {...previous, ...receipt, summary: {...previous.summary, ...receipt.summary}} : receipt;
    if (!Number.isSafeInteger(state.receipt.charts)) void refreshReceipt(true);
    if (Array.isArray(receipt.selected?.catalog)) {
      const rows = receipt.selected.catalog;
      const selected = new Set(rows.map(item => item.catalog_key || `${item.source}:${item.chart_id}`));
      const selectedIds = new Map(rows.filter(item => Number.isSafeInteger(item.candidate_id))
        .map(item => [item.candidate_id, item.catalog_key || `${item.source}:${item.chart_id}`]));
      const selectedArtifacts = new Map(rows.filter(item => artifactKey(item))
        .map(item => [artifactKey(item), item.catalog_key || `${item.source}:${item.chart_id}`]));
      const libraryIds = new Set((receipt.selected.library || []).map(item => item.candidate_id));
      const expansionIds = new Set((receipt.selected.expansion || []).map(item => item.candidate_id));
      const otherIds = new Set([...libraryIds, ...expansionIds]);
      const complete = Array.isArray(receipt.selected.library) && Array.isArray(receipt.selected.expansion);
      const unknown = new Set();
      for (const item of [...state.items, ...state.queue, ...(state.current ? [state.current] : [])]) {
        const priorCrossScope = item.selected === true && ["library", "expansion", "exact"].includes(item.selected_in_scope);
        const canonicalCrossScope = priorCrossScope && !Number.isSafeInteger(item.candidate_id) && item.selection_confirmed_revision === receipt.selection_revision;
        if (complete && priorCrossScope && !Number.isSafeInteger(item.candidate_id) && !canonicalCrossScope) { item.selection_check = true; unknown.add(item.catalog_key); }
        const otherScope = Number.isSafeInteger(item.candidate_id) && otherIds.has(item.candidate_id);
        const canonicalCatalogKey = selected.has(item.catalog_key) ? item.catalog_key
          : selectedIds.get(item.candidate_id) || selectedArtifacts.get(artifactKey(item))
          || (selected.has(item.selected_catalog_key) ? item.selected_catalog_key : null);
        item.selected = Boolean(canonicalCatalogKey) || otherScope
          || (!complete && priorCrossScope) || canonicalCrossScope;
        if (otherScope) { item.selected_in_scope = libraryIds.has(item.candidate_id) ? "library" : "expansion"; item.selected_catalog_key = null; }
        else if (canonicalCatalogKey) { item.selected_in_scope = "catalog"; item.selected_catalog_key = canonicalCatalogKey; }
        if (complete && !item.selected) { item.selected_in_scope = null; item.selected_catalog_key = null; }
      }
      for (const key of unknown) void canonicalChoice(key, receipt.selection_revision);
    }
    render();
  }
  async function canonicalChoice(key, revision, force = false) {
    if (state.choiceChecks.has(key) || !validKey(key) || (!force && state.choiceRevisions.get(key) === revision)) return;
    state.choiceRevisions.set(key, revision);
    const [source, id] = key.split(":");
    const work = api(`/api/catalog/${source}/${id}`); state.choiceChecks.set(key, work);
    try {
      const data = await work;
      if (data.item?.catalog_key !== key || typeof data.snapshot_token !== "string") throw new Error("The chart choice could not be confirmed.");
      if (Number.isSafeInteger(data.selection_revision) && data.selection_revision < (state.receipt?.selection_revision ?? -1)) return;
      const item = {...data.item, snapshot_token: data.snapshot_token, selection_check: false, selection_confirmed_revision: data.selection_revision};
      for (const row of [...state.items, ...state.queue, ...(state.current ? [state.current] : [])]) if (row.catalog_key === key) Object.assign(row, item);
      if (Number.isSafeInteger(data.selection_revision) && data.selection_revision > (state.receipt?.selection_revision ?? -1)) applyReceipt(data);
    } catch (_) { message("This chart's selection is not confirmed. Use Check choice before adding it."); }
    finally {
      state.choiceChecks.delete(key); render();
      if ((state.receipt?.selection_revision ?? revision) > revision && [...state.items, ...state.queue].some(item => item.catalog_key === key && item.selection_check)) void canonicalChoice(key, state.receipt.selection_revision);
    }
  }
  async function refreshReceipt(notify = false) {
    if (document.body.dataset.authenticated !== "true") return false;
    state.receiptNotify ||= notify;
    if (state.receiptPromise) return state.receiptPromise;
    state.receiptPromise = (async () => {
      try {
        const receipt = await api("/api/selection/history"); applyReceipt(receipt);
        if (!Number.isSafeInteger(receipt.charts) || receipt.selection_revision < (state.receipt?.selection_revision ?? -1)) return false;
        state.awaitingSelection = false;
        if (state.receiptNotify) document.dispatchEvent(new CustomEvent("riff:catalog-selection", {detail: receipt}));
        return true;
      } catch (_) { return false; }
      finally { state.receiptPromise = null; state.receiptNotify = false; render(); }
    })();
    return state.receiptPromise;
  }
  function selectionContext() {
    return $("#riffApp") && window.RiffStudioAPI?.context ? window.RiffStudioAPI.context() : {};
  }
  function blocked(item) {
    return state.busy || state.awaitingSelection || document.body.dataset.authenticated !== "true" || !item
      || item.selectable !== true || item.selected === true || item.selection_check || selectionContext().busy || selectionContext().stale;
  }
  function removeBlocked(item) {
    return state.busy || state.awaitingSelection || document.body.dataset.authenticated !== "true"
      || !item?.selected || item.selected_in_scope !== "catalog" || selectionContext().busy || selectionContext().stale;
  }
  function button(text, action, item, disabled = false) {
    const node = document.createElement("button"); node.type = "button";
    node.className = "button button--secondary"; node.textContent = text; node.disabled = disabled;
    node.addEventListener("click", () => action(item)); return node;
  }
  function renderItems() {
    const host = $("#catalogResults");
    const key = JSON.stringify([state.snapshot, state.items.map(item => [item.catalog_key, item.title, item.artist,
      item.charter, item.album, item.year, item.installed, item.preview?.available, item.preview?.reason, item.blocked_reason,
      item.selected, item.selected_in_scope, blocked(item), removeBlocked(item), state.queue.some(row => row.catalog_key === item.catalog_key)])]);
    if (host._catalogRenderKey === key) return;
    host._catalogRenderKey = key; host.replaceChildren();
    for (const [index, item] of state.items.entries()) {
      const card = document.createElement("article"); card.className = "catalog-card"; card.dataset.catalogKey = item.catalog_key;
      const number = document.createElement("span"); number.className = "catalog-track-number"; number.textContent = String(state.offset + index + 1).padStart(2, "0"); number.setAttribute("aria-hidden", "true");
      const heading = document.createElement("div"); heading.className = "catalog-card-heading";
      const title = document.createElement("h3"); title.textContent = item.title || "Untitled chart";
      const info = document.createElement("p"); info.textContent = [item.artist || "Unknown artist", item.album, item.year].filter(Boolean).join(" \u00b7 ");
      const credit = document.createElement("p"); credit.className = "catalog-credit"; credit.textContent = `Chart by ${item.charter || "unknown charter"}`;
      const availability = document.createElement("p"); availability.textContent = item.preview?.available === false
        ? item.preview.reason || "Audio preview unavailable" : "Chart audio is checked when played.";
      if (!item.selectable) availability.textContent += ` ${item.blocked_reason || "This chart cannot enter the ZIP."}`;
      availability.className = "catalog-availability";
      const actions = document.createElement("div"); actions.className = "catalog-card-actions";
      const play = button("Play preview", playItem, item, item.preview?.available === false);
      const queue = button(state.queue.some(row => row.catalog_key === item.catalog_key) ? "Queued" : "Queue", enqueue, item);
      const add = button(item.selected ? "In ZIP selection" : "Add to ZIP", addToZip, item, blocked(item));
      [play, queue, add].forEach((node, index) => { node.dataset.catalogFocus = `${item.catalog_key}:card:${index}`; });
      actions.append(play, queue, add);
      if (item.selected && item.selected_in_scope === "catalog") {
        const remove = button("Remove from ZIP", row => addToZip(row, "clear"), item, removeBlocked(item));
        remove.dataset.catalogFocus = `${item.catalog_key}:card:3`; actions.append(remove);
      }
      heading.append(title, info, credit);
      if (item.installed === true) {
        const badge = document.createElement("span"); badge.className = "catalog-installed"; badge.textContent = "In library";
        badge.title = "Matching song found in the linked .sng inventory; the exact chart version may differ."; heading.append(badge);
      }
      card.append(number, heading, availability, actions); host.append(card);
    }
  }
  function renderQueue() {
    const host = $("#catalogQueueList");
    const key = JSON.stringify([state.index, state.queue.map(item => [item.catalog_key, item.title, item.artist, item.charter, item.automatic])]);
    if (host._catalogRenderKey === key) return;
    host._catalogRenderKey = key; host.replaceChildren();
    if (!state.queue.length) { const empty = document.createElement("li"); empty.textContent = "Your listening queue is empty. Browse charts and choose Queue or Play preview."; host.append(empty); }
    state.queue.forEach((item, index) => {
      const row = document.createElement("li"); row.dataset.current = String(index === state.index); row.dataset.queueKey = item.catalog_key;
      const info = document.createElement("div"), title = document.createElement("strong"), artist = document.createElement("span");
      title.textContent = `${index === state.index ? "Now: " : ""}${item.title}`;
      artist.textContent = `${item.artist} · ${item.charter || "unknown charter"}${item.automatic ? " · Catalog continuation" : ""}`; info.append(title, artist);
      const actions = document.createElement("div"); actions.className = "catalog-queue-actions";
      const play = button("Play", () => startQueue(index), item); play.setAttribute("aria-label", `Play queued chart ${item.title}`);
      const up = button("↑", () => moveQueue(index, -1), item, index === 0); up.setAttribute("aria-label", `Move ${item.title} earlier`);
      const down = button("↓", () => moveQueue(index, 1), item, index === state.queue.length - 1); down.setAttribute("aria-label", `Move ${item.title} later`);
      const remove = button("Remove", () => removeQueue(index), item); remove.setAttribute("aria-label", `Remove ${item.title} from listening queue`);
      [play, up, down, remove].forEach((node, action) => { node.dataset.catalogFocus = `${item.catalog_key}:queue:${action}`; });
      actions.append(play, up, down, remove); row.append(info, actions); host.append(row);
    });
    $("#catalogQueueCount").textContent = $("#catalogDockQueueCount").textContent = count(state.queue.length);
  }
  function renderRecord(item) {
    const key = item?.catalog_key || "", record = $("#catalogVinylButton");
    if (key !== state.recordKey) {
      if (state.recordKey !== null && key) {
        record.classList.remove("swapping"); void record.offsetWidth; record.classList.add("swapping");
        clearTimeout(state.swapTimer); state.swapTimer = setTimeout(() => record.classList.remove("swapping"), 500);
      }
      state.recordKey = key;
    }
    $(".catalog-vinyl-record").dataset.catalogKey = key;
    $("#catalogRecordLabel").textContent = item?.title?.slice(0, 36) || "RIFF";
    const art = item?.artwork_url || item?.art_url || item?.artwork?.url || item?.preview?.artwork_url;
    const safeArt = typeof art === "string" && /^\/api\/companion\/artwork\/enchor\/[1-9]\d*\.jpg$/.test(art);
    const image = $("#catalogRecordArt"); image.hidden = !safeArt || image.dataset.failedSrc === art;
    if (safeArt) { if (image.getAttribute("src") !== art) image.src = art; } else image.removeAttribute("src");
    $("#catalogInstalledBadge").hidden = item?.installed !== true;
    $("#catalogInstalledBadge").title = "Matching song found in the linked .sng inventory; the exact chart version may differ.";
    if (state.recordPlayback !== state.playback) {
      state.recordPlayback = state.playback;
      document.dispatchEvent(new CustomEvent("riff:catalog-playback", {detail: {state: state.playback}}));
    }
  }
  function renderSound() {
    const mute = $("#catalogMuteButton"); mute.setAttribute("aria-pressed", String(audio.muted));
    mute.setAttribute("aria-label", audio.muted ? "Unmute preview" : "Mute preview"); mute.title = audio.muted ? "Unmute preview" : "Mute preview";
    $("#catalogVolumeWaves").setAttribute("d", audio.muted ? "m17 9 5 6m0-6-5 6" : "M16 8c2 2 2 6 0 8m3-11c4 4 4 10 0 14");
  }
  function render() {
    const focusKey = document.activeElement?.dataset?.catalogFocus;
    const item = state.current;
    $("#catalogNowTitle").textContent = item?.title || "Discover your next chart";
    $("#catalogNowArtist").textContent = item ? [item.artist || "Unknown artist", item.album, item.year].filter(Boolean).join(" \u00b7 ") : "Browse the full chart catalog";
    $("#catalogPlayerDock").dataset.playbackState = state.playback;
    $("#catalogPlayerDock").dataset.previewError = String(state.previewError);
    renderRecord(item); renderSound();
    const canPause = Boolean(audio.getAttribute("src") && !audio.paused);
    const loading = state.playback === "loading";
    $("#catalogVinylButton").disabled = !loading && (!item || item.preview?.available === false);
    $("#catalogVinylButton").setAttribute("aria-label", canPause ? "Pause preview" : loading ? "Cancel preview loading" : "Play preview");
    $("#catalogVinylButton").title = canPause ? "Pause preview" : loading ? "Cancel preview loading" : item?.preview?.available === false ? item.preview.reason || "Audio preview unavailable" : "Play preview";
    $(".catalog-vinyl-symbol").textContent = canPause ? "\u275a\u275a" : loading ? "\u00d7" : "\u25b6";
    $("#catalogPlayerPrevious").disabled = state.index <= 0;
    $("#catalogPlayerNext").disabled = state.feedLoading || state.index < 0
      || (state.index + 1 >= state.queue.length && (!state.continueCatalog || state.feed?.exhausted));
    $("#catalogPlayerNext").setAttribute("aria-label", state.index + 1 < state.queue.length ? "Next queued chart" : "Next chart from catalog");
    $("#catalogContinueCatalog").checked = state.continueCatalog;
    $("#catalogClearQueue").disabled = state.queue.length === 0 && !loading;
    $("#catalogContinuationContext").textContent = state.feed
      ? `Continue ${state.feed.seed ? "the captured shuffled order" : "in catalog order"}${state.feed.q ? ` for "${state.feed.q}"` : ""}${state.feed.decade ? ` from the ${state.feed.decade}s` : ""}. Queued picks play first. Only the current chart's audio is loaded.`
      : "After you press Play, queued picks play first, then more charts from the current catalog search. Only the current chart's audio is loaded.";
    $("#catalogAddButton").disabled = item?.selection_check || state.awaitingSelection ? state.busy || document.body.dataset.authenticated !== "true" : blocked(item);
    $("#catalogAddButton").textContent = state.awaitingSelection ? "Check choices" : item?.selection_check ? "Check choice" : item?.selected ? "In ZIP selection" : "Add to ZIP";
    $("#catalogReviewZipButton").disabled = document.body.dataset.authenticated !== "true" || state.busy || selectionContext().busy || !(state.receipt?.charts > 0 || selectionContext().charts > 0);
    $("#catalogRemoveButton").hidden = !item?.selected || item.selected_in_scope !== "catalog";
    $("#catalogRemoveButton").disabled = removeBlocked(item);
    renderItems(); renderQueue(); measure();
    if (focusKey) {
      const target = [...root.querySelectorAll("[data-catalog-focus]")].find(node => node.dataset.catalogFocus === focusKey && !node.disabled);
      (target || $("#catalogQueueButton")).focus({preventScroll: true});
    }
  }
  function measure() {
    const timeline = $(".catalog-timeline");
    const target = matchMedia("(max-width: 800px)").matches ? $("#catalogMobileAudioSettings") : $("#catalogPlayerDock");
    if (timeline.parentElement !== target) target.append(timeline);
    const height = Math.ceil($("#catalogPlayerDock").getBoundingClientRect().height);
    document.documentElement.style.setProperty("--catalog-dock-height", `${height}px`);
  }
  function drawer(tab = "catalog", opener = null) {
    if ($("#catalogDrawer").hidden) state.opener = opener || document.activeElement;
    state.tab = tab;
    $("#catalogDrawer").hidden = false;
    document.body.dataset.catalogDrawerOpen = "true";
    const site = tab === "site", queue = tab === "queue";
    $("#catalogBrowsePanel").hidden = site || queue; $("#catalogQueuePanel").hidden = site || !queue;
    $("#catalogSitePage").hidden = !site; $(".catalog-tabs").hidden = site;
    $("#catalogSiteNav").hidden = !site;
    root.setAttribute("role", "dialog"); root.setAttribute("aria-modal", "true");
    for (const node of document.querySelectorAll("#main-content,.site-header,.site-footer,.ad-wrap,.cause-bar")) node.inert = true;
    $("#catalogDrawerTitle").textContent = site ? "Riff" : queue ? "Listening queue" : "Browse all charts";
    $("#catalogTab").setAttribute("aria-pressed", String(!queue)); $("#catalogQueueTab").setAttribute("aria-pressed", String(queue));
    for (const id of ["#catalogBrowseButton", "#catalogQueueButton"]) $(id).setAttribute("aria-expanded", "true");
    (tab === "catalog" ? $("#catalogSearch") : $("#catalogCloseButton")).focus({preventScroll: true});
    if (tab === "catalog" && !state.items.length && !state.loading) void browsePage(0);
    renderQueue();
  }
  function closeDrawer(restoreFocus = true) {
    $("#catalogDrawer").hidden = true; delete document.body.dataset.catalogDrawerOpen;
    root.removeAttribute("role"); root.removeAttribute("aria-modal");
    for (const node of document.querySelectorAll("#main-content,.site-header,.site-footer,.ad-wrap,.cause-bar")) node.inert = false;
    for (const id of ["#catalogBrowseButton", "#catalogQueueButton"]) $(id).setAttribute("aria-expanded", "false");
    if (restoreFocus && state.opener?.isConnected) state.opener.focus({preventScroll: true});
  }
  function leaveDrawer() {
    const site = state.tab === "site"; closeDrawer();
    if (site && location.pathname !== state.basePath) { history.pushState({riffPage: state.basePath}, "", state.basePath); document.title = state.baseTitle; }
  }
  async function browsePage(offset = 0) {
    const request = ++state.request; state.controller?.abort(); state.controller = new AbortController();
    state.loading = true; $("#catalogResults").setAttribute("aria-busy", "true");
    $("#catalogRetryButton").hidden = true; $("#catalogPreviousPage").disabled = $("#catalogNextPage").disabled = true;
    message("Taking the records off the shelf.", "#catalogBrowseStatus");
    const params = new URLSearchParams({limit: String(LIMIT), offset: String(offset), q: state.q,
      order: state.seed ? "shuffle" : "source"});
    if (state.decade) params.set("decade", state.decade);
    if (state.seed) params.set("seed", state.seed);
    if (state.snapshot && offset > 0) params.set("snapshot_token", state.snapshot);
    try {
      const data = await api(`/api/catalog?${params}`, {signal: state.controller.signal});
      if (request !== state.request) return;
      if (data.status !== "ready") {
        message(data.readiness?.message || "The catalog is being prepared. Check again in a moment.", "#catalogBrowseStatus");
        $("#catalogCounts").textContent = "Chart and song counts await catalog confirmation.";
        $("#catalogRetryButton").hidden = false; return;
      }
      if (!Array.isArray(data.items) || data.items.length > LIMIT || data.offset !== offset
          || typeof data.has_more !== "boolean" || typeof data.snapshot_token !== "string"
          || ![data.total_charts, data.total_songs, data.total_filtered_charts].every(value => Number.isSafeInteger(value) && value >= 0)) throw new Error("The catalog page could not be confirmed.");
      state.items = data.items.filter(item => validKey(item?.catalog_key)); state.offset = offset;
      state.total = data.total_charts; state.filtered = data.total_filtered_charts; state.snapshot = data.snapshot_token;
      applyReceipt(data); renderItems();
      $("#catalogCounts").textContent = `${count(data.total_charts)} indexed charts \u00b7 ${count(data.total_songs)} unique songs${state.q || state.decade ? ` \u00b7 ${count(data.total_filtered_charts)} matching charts` : ""}. Every catalog page is browsable.`;
      $("#catalogPagePosition").textContent = `${count(offset + (state.items.length ? 1 : 0))}–${count(offset + state.items.length)} of ${count(state.filtered)}`;
      $("#catalogPreviousPage").disabled = offset === 0; $("#catalogNextPage").disabled = !data.has_more;
      message(state.items.length ? "Choose Play preview, Queue, or Add to ZIP." : "No charts match this search.", "#catalogBrowseStatus");
      return true;
    } catch (error) {
      if (request !== state.request || error.name === "AbortError") return;
      message(error.status === 409 ? "The installed catalog changed. Check again to browse its current pages." : error.message || "Catalog unavailable. Your listening queue is preserved.", "#catalogBrowseStatus");
      $("#catalogRetryButton").hidden = false;
      if (error.status === 409) state.snapshot = null;
      return false;
    } finally { if (request === state.request) { state.loading = false; $("#catalogResults").setAttribute("aria-busy", "false"); } }
  }
  function enqueue(item) {
    let index = state.queue.findIndex(row => row.catalog_key === item.catalog_key);
    if (index >= 0) {
      state.queue[index] = {...state.queue[index], ...item, automatic: false};
      const firstAutomatic = state.queue.findIndex((row, position) => position > state.index && row.automatic);
      if (index > state.index && firstAutomatic >= 0 && firstAutomatic < index) {
        const [queued] = state.queue.splice(index, 1); state.queue.splice(firstAutomatic, 0, queued); index = firstAutomatic;
      }
      save(); render(); return index;
    }
    pruneConsumed(1);
    if (state.queue.length >= QUEUE_LIMIT) { message(`The listening queue holds ${QUEUE_LIMIT} charts. Remove an item before queueing another.`); return -1; }
    index = state.queue.findIndex((row, position) => position > state.index && row.automatic);
    if (index < 0) index = state.queue.length;
    state.queue.splice(index, 0, {...item, automatic: false}); save(); render(); message(`Queued ${item.title}. ZIP choices are unchanged.`); return index;
  }
  function pruneConsumed(incoming) {
    const remove = Math.min(Math.max(0, state.queue.length + incoming - QUEUE_LIMIT), Math.max(0, state.index));
    if (remove) { state.queue.splice(0, remove); state.index -= remove; }
  }
  function captureFeed(item) {
    const position = state.items.findIndex(row => row.catalog_key === item.catalog_key);
    state.feed = {q: state.q, decade: state.decade, seed: state.seed, offset: position >= 0 ? state.offset + position + 1 : 0,
      snapshot: state.snapshot, exhausted: false};
  }
  async function startQueue(index, capture = true) {
    if (index < 0 || index >= state.queue.length) return false;
    if (capture || !state.feed) captureFeed(state.queue[index]);
    state.continuationActive = true; state.autoFailures = 0;
    return selectQueue(index, true);
  }
  async function advanceQueue(play = false, automatic = false) {
    if (state.feedLoading || state.index < 0) return false;
    if (state.index + 1 < state.queue.length) return selectQueue(state.index + 1, play, automatic);
    if (!state.continueCatalog || state.feed?.exhausted || (automatic && !state.continuationActive)) {
      state.continuationActive = false;
      message(state.feed?.exhausted ? "This catalog order has ended. Browse or Shuffle to choose a new order." : "The listening queue has ended.");
      render(); return false;
    }
    if (!state.feed) captureFeed(state.current);
    stopMedia(false);
    const feed = state.feed, epoch = ++state.loadEpoch, controller = new AbortController();
    state.loadController?.abort(); state.loadController = controller; state.feedLoading = true;
    state.playback = "loading"; render(); message("Finding the next charts in your captured catalog order…");
    try {
      // Only metadata is fetched here. The audio reader opens one chart after it is chosen.
      for (let pages = 0; pages < 3; pages += 1) {
        const offset = feed.offset;
        const params = new URLSearchParams({limit: String(CONTINUATION_BATCH), offset: String(offset), q: feed.q,
          order: feed.seed ? "shuffle" : "source"});
        if (feed.decade) params.set("decade", feed.decade);
        if (feed.seed) params.set("seed", feed.seed);
        if (feed.snapshot) params.set("snapshot_token", feed.snapshot);
        const data = await api(`/api/catalog?${params}`, {signal: controller.signal});
        if (epoch !== state.loadEpoch) return false;
        if (data.status !== "ready") throw new Error(data.readiness?.message || "The catalog is not ready. Check it from Browse before continuing.");
        if (!Array.isArray(data.items) || data.items.length > CONTINUATION_BATCH || data.offset !== offset
            || typeof data.has_more !== "boolean" || typeof data.snapshot_token !== "string"
            || !Number.isSafeInteger(data.total_filtered_charts) || data.total_filtered_charts < 0)
          throw new Error("The next catalog page could not be confirmed.");
        const items = data.items.filter(item => validKey(item?.catalog_key));
        feed.snapshot = data.snapshot_token; feed.offset = offset + data.items.length; feed.exhausted = !data.has_more;
        if (!data.items.length && data.has_more) throw new Error("The next catalog page was empty. Check the catalog before continuing.");
        // Explicit picks queued while this read was in flight take priority.
        const keys = new Set(state.queue.map(item => item.catalog_key));
        const incoming = items.filter(item => !keys.has(item.catalog_key));
        pruneConsumed(incoming.length);
        for (const item of incoming) if (state.queue.length < QUEUE_LIMIT)
          state.queue.push({...item, snapshot_token: data.snapshot_token, automatic: true});
        applyReceipt(data); save();
        if (state.index + 1 < state.queue.length) {
          state.feedLoading = false;
          if (epoch !== state.loadEpoch) return false;
          return await selectQueue(state.index + 1, play, automatic);
        }
        if (feed.exhausted) break;
      }
      state.continuationActive = false; state.playback = "paused";
      message(feed.exhausted ? "This catalog order has ended. Browse or Shuffle to choose a new order."
        : "These catalog pages were already queued. Press Next to check the following page.");
      return false;
    } catch (error) {
      if (epoch !== state.loadEpoch || error.name === "AbortError") return false;
      state.continuationActive = false; state.playback = "paused";
      message(error.status === 409 ? "The catalog changed. Browse or Shuffle to choose a fresh order."
        : `Catalog continuation paused: ${error.message || "the next page is unavailable"}. Your listening queue is preserved.`);
      return false;
    } finally {
      if (epoch === state.loadEpoch) { state.feedLoading = false; state.loadController = null; save(); render(); }
    }
  }
  function moveQueue(index, delta) {
    const key = state.current?.catalog_key, target = index + delta;
    if (target < 0 || target >= state.queue.length) return;
    [state.queue[index], state.queue[target]] = [state.queue[target], state.queue[index]];
    state.index = state.queue.findIndex(item => item.catalog_key === key); save(); render();
  }
  function removeQueue(index) {
    if (index === state.index) stopMedia();
    state.queue.splice(index, 1);
    if (index < state.index) state.index -= 1;
    else if (index === state.index) { state.index = Math.min(index, state.queue.length - 1); state.current = state.queue[state.index] || null; }
    save(); render();
  }
  async function detail(key, signal) {
    if (!validKey(key)) throw new Error("This chart identity cannot be previewed.");
    const [source, id] = key.split(":");
    const data = await api(`/api/catalog/${source}/${id}`, {signal});
    if (data.item?.catalog_key !== key || typeof data.snapshot_token !== "string") throw new Error("This chart could not be confirmed in the installed catalog.");
    applyReceipt(data); return {...data.item, snapshot_token: data.snapshot_token};
  }
  function stopMedia(cancelContinuation = true) {
    state.loadEpoch += 1; state.loadController?.abort(); state.loadController = null; state.feedLoading = false;
    state.lookupKey = null;
    if (cancelContinuation) state.continuationActive = false;
    audio.pause(); audio.removeAttribute("src"); audio.load();
    state.resource?.cleanup?.(); state.resource = null; state.loadedKey = null; state.playback = "paused"; state.previewError = false;
    updateTime(); render();
  }
  async function selectQueue(index, play = false, automatic = false) {
    if (index < 0 || index >= state.queue.length) return false;
    stopMedia(false); state.index = index; state.current = state.queue[index]; save(); render();
    const key = state.current.catalog_key;
    if (!play) { message(state.current.preview?.available === false ? state.current.preview.reason || "Audio preview unavailable for this chart. Listening queue and ZIP choices still work." : "Preview paused. Press Play to load this chart's audio."); return true; }
    const epoch = ++state.loadEpoch, controller = new AbortController(); state.loadController = controller;
    state.playback = "loading"; render(); message("Checking chart audio…");
    try {
      const item = await detail(key, controller.signal);
      if (epoch !== state.loadEpoch || state.current?.catalog_key !== key) return false;
      const position = state.queue.findIndex(row => row.catalog_key === key);
      if (position < 0) return false;
      state.index = position; state.current = state.queue[position] = {...item, automatic: state.queue[position].automatic}; render();
      if (item.preview?.available === false) throw new Error(item.preview.reason || "Audio preview unavailable for this chart.");
      if (!window.RiffCatalogAudio?.load) throw new Error("The audio preview reader is unavailable. Browse and ZIP choices still work.");
      const resource = await window.RiffCatalogAudio.load(item, {signal: controller.signal, localUrl: item.preview?.local_url ?? null,
        onProgress: text => { if (epoch === state.loadEpoch) message(String(text)); }});
      if (epoch !== state.loadEpoch || state.current?.catalog_key !== key) { resource.cleanup?.(); return false; }
      if (!/^blob:/.test(resource.url || "")) { resource.cleanup?.(); throw new Error("The chart audio could not be safely opened."); }
      state.resource = resource; state.loadedKey = item.catalog_key; audio.src = resource.url; audio.load();
      audio.addEventListener("loadedmetadata", () => {
        if (epoch !== state.loadEpoch) return;
        const start = Math.max(0, Number(resource.previewStartMs || 0) / 1000);
        if (start && Number.isFinite(audio.duration)) audio.currentTime = Math.min(start, Math.max(0, audio.duration - .1));
        updateTime();
      }, {once: true});
      await audio.play();
      if (epoch !== state.loadEpoch) return false;
      state.autoFailures = 0; save(); return true;
    } catch (error) {
      if (epoch !== state.loadEpoch || error.name === "AbortError") return false;
      audio.pause(); audio.removeAttribute("src"); audio.load();
      state.resource?.cleanup?.(); state.resource = null; state.loadedKey = null;
      state.playback = "paused"; state.previewError = error.name !== "NotAllowedError"; render();
      message(error.name === "NotAllowedError" ? "Your browser paused playback. Press Play to start the preview." : error.message || "This chart's audio preview could not be opened.");
      if (automatic && state.continuationActive && error.name !== "NotAllowedError") {
        if (++state.autoFailures < 3) return advanceQueue(true, true);
        message("Three previews could not be played. Continuation paused. Choose another chart or press Play to try again.");
      }
      state.continuationActive = false;
      return false;
    } finally { if (epoch === state.loadEpoch) state.loadController = null; }
  }
  async function playItem(item) { const index = enqueue(item); return index >= 0 ? startQueue(index) : false; }
  async function playKey(key) {
    if (!validKey(key)) throw new Error("This chart identity cannot be previewed.");
    stopMedia();
    const epoch = ++state.loadEpoch, controller = new AbortController();
    state.loadController = controller; state.lookupKey = key; state.continuationActive = true;
    state.playback = "loading"; render(); message("Checking the selected chart's audio…");
    try {
      const item = await detail(key, controller.signal);
      if (epoch !== state.loadEpoch || controller.signal.aborted || state.lookupKey !== key) return false;
      state.lookupKey = null; state.loadController = null;
      return await playItem(item);
    } catch (error) {
      if (epoch !== state.loadEpoch || error.name === "AbortError") return false;
      state.continuationActive = false; state.playback = "paused"; state.previewError = true;
      message(error.message || "This chart's preview could not be opened.");
      throw error;
    } finally {
      if (epoch === state.loadEpoch) { state.lookupKey = null; state.loadController = null; render(); }
    }
  }
  async function togglePlay() {
    if (state.playback === "loading") { stopMedia(); message("Preview loading cancelled. Press Play to try again."); return; }
    if (!state.current) { drawer("catalog", $("#catalogVinylButton")); return; }
    if (!audio.paused) { state.loadEpoch += 1; state.loadController = null; state.continuationActive = false; audio.pause(); return; }
    if (state.loadedKey === state.current.catalog_key && audio.src) {
      state.previewError = false; render();
      state.continuationActive = true; state.autoFailures = 0;
      try { await audio.play(); } catch (_) { state.continuationActive = false; message("Your browser paused playback. Press Play to retry."); } return;
    }
    await startQueue(state.index, false);
  }
  async function shufflePlayback() {
    const play = !audio.paused || (state.playback === "loading" && state.continuationActive);
    stopMedia(false);
    const epoch = state.loadEpoch;
    state.seed = newSeed(); state.snapshot = null; state.continuationActive = play;
    state.playback = "loading"; render(); message("Finding your next record in a fresh catalog order.");
    const ready = await browsePage(0);
    if (epoch !== state.loadEpoch) return;
    if (!ready || !state.items.length) {
      state.playback = "paused"; state.continuationActive = false; render();
      message(ready ? "No charts match this search. Change the title, artist, album or decade." : "Shuffle could not be confirmed. Your listening queue is preserved.");
      return;
    }
    const first = state.items.find(item => item.catalog_key !== state.current?.catalog_key) || state.items[0];
    const index = enqueue(first);
    if (index < 0) { state.playback = "paused"; state.continuationActive = false; render(); return; }
    captureFeed(first); await selectQueue(index, play);
  }
  async function addToZip(item, action = "select") {
    if (action === "clear" ? removeBlocked(item) : blocked(item)) return;
    const bridge = $("#riffApp") ? window.RiffStudioAPI : null;
    const held = Boolean(bridge?.begin);
    if (held && !bridge.begin()) return;
    state.busy = true; render(); message(action === "clear" ? "Removing this chart from your ZIP selection…" : "Saving this chart to your ZIP selection…");
    try {
      if (action !== "clear" && !item.snapshot_token) item = await detail(item.catalog_key);
      const context = selectionContext();
      const revision = Number.isSafeInteger(context.selection_revision) ? context.selection_revision : state.receipt?.selection_revision;
      const generation = Number.isSafeInteger(context.exact_generation_id) ? context.exact_generation_id : state.receipt?.exact_generation_id;
      const result = await api("/api/catalog/selection", {method: "POST", body: {
        catalog_key: action === "clear" && validKey(item.selected_catalog_key) ? item.selected_catalog_key : item.catalog_key,
        ...(action === "clear" ? {} : {snapshot_token: item.snapshot_token || state.snapshot}), action,
        ...(Number.isSafeInteger(revision) ? {selection_revision: revision} : {}),
        ...(Number.isSafeInteger(generation) ? {exact_generation_id: generation} : {})}});
      applyReceipt(result); document.dispatchEvent(new CustomEvent("riff:catalog-selection", {detail: result}));
      if (result.already_selected) {
        for (const row of [...state.items, ...state.queue]) if (row.catalog_key === item.catalog_key) { row.selected = true; row.selected_in_scope = result.selected_in_scope; }
        if (state.current?.catalog_key === item.catalog_key) { state.current.selected = true; state.current.selected_in_scope = result.selected_in_scope; }
      }
      message(action === "clear" ? `Removed ${item.title} from ZIP choices. Listening continues. ${result.undo?.available ? "Undo restores the previous selection." : result.undo?.reason || "This change has no undo receipt."}`
        : document.body.dataset.riffStage === "done" ? `Added ${item.title} for your next ZIP. The prepared ZIP contains its earlier choices.` : `Added ${item.title} to your ZIP selection. Listening continues.`);
    } catch (error) {
      const checked = await refreshReceipt(true);
      state.awaitingSelection = !checked;
      if (!checked && held) bridge.stale("The catalog add result is not confirmed. Check selection status before another change.");
      message(!checked ? "This add could not be confirmed, and selection status is unavailable. Check choices before another add; no automatic retry was sent."
        : error.status === 409 ? "Choices are busy or changed. Current selections were checked; review them before adding again." : "This add could not be confirmed. Current selections were checked; no automatic add retry was sent.");
    } finally { state.busy = false; if (held) bridge.finish(); render(); if (action === "clear" && document.activeElement === $("#catalogRemoveButton") && $("#catalogRemoveButton").hidden) $("#catalogQueueButton").focus({preventScroll: true}); }
  }
  function updateTime() {
    const duration = Number.isFinite(audio.duration) ? audio.duration : 0;
    const seek = $("#catalogSeek"); seek.disabled = !duration; seek.max = String(Math.floor(duration)); seek.value = String(Math.floor(audio.currentTime || 0));
    $("#catalogTime").textContent = `${time(audio.currentTime || 0)} / ${duration ? time(duration) : "—"}`;
  }
  async function softPage(path, push = true) {
    if (!["/", "/about", "/terms", "/privacy", "/dmca"].includes(path)) return false;
    const request = ++state.pageRequest;
    if (path === "/" && $("#riffApp")) { closeDrawer(); document.title = "Riff | Signal Check"; if (push) history.pushState({riffPage: path}, "", path); state.sourcePage = path; return true; }
    drawer("site"); message("Opening this Riff page…", "#catalogDrawerTitle");
    try {
      const response = await fetch(path, {credentials: "same-origin", headers: {"Accept": "text/html"}});
      if (!response.ok || request !== state.pageRequest) throw new Error("This page could not be opened.");
      const documentPage = new DOMParser().parseFromString(await response.text(), "text/html");
      const main = documentPage.querySelector("#main-content"); if (!main) throw new Error("This Riff page could not be confirmed.");
      const authenticated = documentPage.body.dataset.authenticated;
      if (authenticated !== document.body.dataset.authenticated || documentPage.body.dataset.riffOwner !== document.body.dataset.riffOwner) {
        stop(true); document.body.dataset.authenticated = authenticated;
        location.assign(path); return false;
      }
      main.querySelectorAll("script,iframe,object,embed").forEach(node => node.remove());
      if (path !== "/") main.querySelectorAll("form").forEach(node => node.remove());
      if (path === "/") {
        // Home is application-owned HTML. Forms are needed when mounting the journey.
        const home = documentPage.querySelector("#riffApp");
        if (!home || !window.RiffJourney?.mount) throw new Error("Open Riff home directly to resume the music journey.");
        $("#main-content").replaceChildren(home); closeDrawer(false); window.RiffJourney.mount(); window.RiffStudio?.mount(); window.RiffScene?.mount(home);
        state.basePath = "/"; state.baseTitle = documentPage.title;
      } else {
        $("#catalogSitePage").replaceChildren(...main.childNodes);
        $("#catalogDrawerTitle").textContent = documentPage.title.split("|")[0].trim();
        $("#catalogSitePage").focus({preventScroll: true});
      }
      document.title = documentPage.title; state.sourcePage = path;
      if (push) history.pushState({riffPage: path}, "", path);
      return true;
    } catch (error) { $("#catalogSitePage").textContent = error.message || "This page could not be opened. Your player is preserved."; return false; }
  }
  function stop(clear = false) {
    stopMedia(); if (clear) { state.queue = []; state.index = -1; state.current = null; state.feed = null; try { sessionStorage.removeItem(storageKey); } catch (_) {} render(); }
  }
  $("#catalogBrowseButton").addEventListener("click", event => drawer("catalog", event.currentTarget));
  $("#catalogQueueButton").addEventListener("click", event => drawer("queue", event.currentTarget));
  $("#catalogCloseButton").addEventListener("click", leaveDrawer);
  $("#catalogTab").addEventListener("click", () => drawer("catalog")); $("#catalogQueueTab").addEventListener("click", () => drawer("queue"));
  function searchCatalog() {
    state.q = $("#catalogSearch").value.trim().slice(0, 200); state.decade = $("#catalogDecade").value;
    state.seed = ""; state.snapshot = null; void browsePage(0);
  }
  $("#catalogSearchForm").addEventListener("submit", event => { event.preventDefault(); searchCatalog(); });
  $("#catalogDecade").addEventListener("change", searchCatalog);
  $("#catalogShuffleButton").addEventListener("click", () => { const bytes = new Uint8Array(8); crypto.getRandomValues(bytes); state.seed = [...bytes].map(value => value.toString(16).padStart(2, "0")).join(""); state.snapshot = null; void browsePage(0); });
  $("#catalogRetryButton").addEventListener("click", () => void browsePage(0));
  $("#catalogPreviousPage").addEventListener("click", () => void browsePage(Math.max(0, state.offset - LIMIT)));
  $("#catalogNextPage").addEventListener("click", () => void browsePage(state.offset + LIMIT));
  $("#catalogVinylButton").addEventListener("click", togglePlay);
  $("#catalogPlayerShuffle").addEventListener("click", () => void shufflePlayback());
  $("#catalogMuteButton").addEventListener("click", () => { audio.muted = !audio.muted; renderSound(); save(); });
  $("#catalogRecordArt").addEventListener("error", event => { event.target.dataset.failedSrc = event.target.getAttribute("src"); event.target.hidden = true; });
  $("#catalogPlayerPrevious").addEventListener("click", () => { const play = !audio.paused || (state.playback === "loading" && state.continuationActive); state.continuationActive = play; state.autoFailures = 0; void selectQueue(state.index - 1, play); });
  $("#catalogPlayerNext").addEventListener("click", () => { const play = !audio.paused || (state.playback === "loading" && state.continuationActive); state.continuationActive = play; state.autoFailures = 0; void advanceQueue(play, play); });
  $("#catalogContinueCatalog").addEventListener("change", event => {
    state.continueCatalog = event.target.checked;
    if (!state.continueCatalog && state.feedLoading) { stopMedia(); message("Catalog continuation cancelled. Queued picks are preserved."); }
    save(); render();
  });
  $("#catalogAddButton").addEventListener("click", async () => {
    if (state.awaitingSelection) { const checked = await refreshReceipt(true); message(checked ? "Current ZIP selections confirmed. Review them before another add." : "Selection status is still unavailable. Check choices before another add."); }
    else if (state.current?.selection_check) void canonicalChoice(state.current.catalog_key, state.receipt?.selection_revision, true);
    else void addToZip(state.current);
  });
  $("#catalogClearQueue").addEventListener("click", () => { stop(true); save(); message("Listening queue cleared. ZIP choices are unchanged."); });
  $("#catalogRemoveButton").addEventListener("click", () => void addToZip(state.current, "clear"));
  $("#catalogReviewZipButton").addEventListener("click", async () => {
    closeDrawer(false);
    if (!$("#riffApp") && !await softPage("/")) return;
    if (!document.body.dataset.riffStage) {
      const ready = await new Promise(resolve => {
        const observer = new MutationObserver(() => { if (document.body.dataset.riffStage) { clearTimeout(timer); observer.disconnect(); resolve(true); } });
        const timer = setTimeout(() => { observer.disconnect(); resolve(false); }, 10000);
        observer.observe(document.body, {attributes: true, attributeFilter: ["data-riff-stage"]});
      });
      if (!ready) { message("Home is still loading. Review the ZIP after the journey is ready."); return; }
    }
    document.dispatchEvent(new CustomEvent("riff:catalog-review-zip"));
  });
  $("#catalogVolume").addEventListener("input", event => { audio.volume = Number(event.target.value); save(); }); audio.volume = .7;
  $("#catalogSeek").addEventListener("input", event => { if (Number.isFinite(audio.duration)) audio.currentTime = Math.min(Number(event.target.value), audio.duration); });
  audio.addEventListener("playing", () => { state.playback = "playing"; state.previewError = false; message(state.resource?.previewKind === "mixed-clip" ? `${Math.round(Number(state.resource.durationHint || 45000) / 1000)}s chart preview · mixed audio clip. ZIP choices are separate.` : "Playing chart audio. ZIP choices are separate."); render(); });
  audio.addEventListener("pause", () => { if (state.playback !== "loading") { state.playback = "paused"; render(); } });
  audio.addEventListener("waiting", () => { if (audio.src) { state.playback = "buffering"; message("Buffering chart audio…"); render(); } });
  audio.addEventListener("timeupdate", updateTime); audio.addEventListener("durationchange", updateTime);
  audio.addEventListener("error", () => { if (audio.src) { state.playback = "paused"; state.previewError = true; if (!state.loadController) state.continuationActive = false; message("This browser could not play the chart audio. Try another chart or check its audio format."); render(); } });
  audio.addEventListener("ended", () => {
    state.playback = "paused"; render();
    if (state.continuationActive) { state.autoFailures = 0; void advanceQueue(true, true); }
    else message("Preview ended. Press Play or Next to continue listening.");
  });
  document.addEventListener("keydown", event => {
    if ($("#catalogDrawer").hidden) return;
    if (event.key === "Escape") { event.preventDefault(); leaveDrawer(); }
    if (event.key === "Tab") {
      const controls = [...root.querySelectorAll("button,a[href],input,select,[tabindex='0']")].filter(node => !node.disabled && node.getClientRects().length);
      const first = controls[0], last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
  });
  document.addEventListener("riff:selection-state", () => render());
  document.addEventListener("riff:selection-committed", event => applyReceipt(event.detail));
  document.addEventListener("riff:catalog-state", event => applyReceipt(event.detail));
  document.addEventListener("click", event => {
    const link = event.target.closest("a[href]"); if (!link || event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || link.target || link.hasAttribute("download")) return;
    const url = new URL(link.href, location.href); if (url.origin !== location.origin || url.search || url.hash || !["/", "/about", "/terms", "/privacy", "/dmca"].includes(url.pathname)) return;
    event.preventDefault(); void softPage(url.pathname);
  });
  $("#logoutButton")?.addEventListener("click", () => stop(true), {capture: true});
  window.addEventListener("popstate", () => void softPage(location.pathname, false));
  window.addEventListener("resize", measure); window.visualViewport?.addEventListener("resize", measure);
  new ResizeObserver(measure).observe($("#catalogPlayerDock"));
  const motionToggle = $("#riff-motion-toggle");
  const syncMotion = () => { $("#catalogPlayerDock").dataset.motion = motionToggle?.getAttribute("aria-pressed") === "true" ? "on" : "off"; };
  if (motionToggle) new MutationObserver(syncMotion).observe(motionToggle, {attributes: true, attributeFilter: ["aria-pressed"]});
  syncMotion();
  window.addEventListener("pagehide", () => { save(); stopMedia(); });
  window.RiffCatalog = Object.freeze({browse: () => drawer("catalog"), stop, playKey, intentToken: () => state.loadEpoch});
  restore(); render(); void refreshReceipt();
})();
