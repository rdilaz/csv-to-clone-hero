(() => {
  "use strict";

  const $ = selector => document.querySelector(selector);
  const panel = $("#studio-panel");
  const bridge = window.RiffStudioAPI;
  if (!panel || !bridge || document.body.dataset.authenticated !== "true") return;
  const PAGE_SIZE = 100;
  const state = {
    epoch: 0, contextKey: "", items: [], order: [], cursor: -1,
    total: 0, loaded: false, complete: false, loading: false,
    token: null, request: null, controller: null, seed: null, busy: false,
    preview: false, playerId: null, selectionSerial: 0,
    operationPreview: null, previewKey: "", previewRequest: 0,
  };
  const status = (message, error = false) => {
    const node = $("#studio-deck-status");
    if (node) { node.textContent = message; node.classList.toggle("is-error", error); }
  };
  const scope = () => ["library", "expansion", "both"].includes($("#studio-select-scope")?.value) ? $("#studio-select-scope").value : "both";
  const current = () => state.items[state.order[state.cursor]] || null;
  const skipped = item => Boolean($("#studio-skip-selected")?.checked && (item.song_selected ?? item.selected));
  const spotifyId = item => /^[a-zA-Z0-9]{22}$/.test(item?.spotify_id || "") ? item.spotify_id : null;
  const formatCount = count => Number(count || 0).toLocaleString();

  function reset(message = "Shuffle or browse your eligible songs. Nothing plays until you open the Spotify player.") {
    state.epoch += 1;
    state.controller?.abort();
    Object.assign(state, {items: [], order: [], cursor: -1, total: 0, loaded: false, complete: false,
      loading: false, token: null, request: null, controller: null, seed: null, preview: false, playerId: null});
    const player = $("#studio-player-host");
    player?.replaceChildren();
    if (player) player.hidden = true;
    render();
    status(message);
  }

  function syncControls() {
    const context = bridge.context();
    const allStatus = $("#studio-select-status");
    if (allStatus && context.known && /Loading your chart pool/.test(allStatus.textContent)) allStatus.textContent = "Ready · selections include every page.";
    const blocked = state.busy || context.busy || context.stale;
    ["#studio-select-scope", "#studio-select-policy", "#studio-include-uncertain"].forEach(selector => {
      const node = $(selector);
      if (node) node.disabled = blocked;
    });
    $("#studio-select-all").disabled = blocked || !state.operationPreview || state.previewKey !== previewKey();
    ["#studio-next", "#studio-shuffle"].forEach(selector => {
      const node = $(selector);
      if (node) node.disabled = state.loading || state.busy || context.busy;
    });
    const item = current();
    const add = $("#studio-add");
    if (add) { add.disabled = blocked || !item || item.selected || !item.selectable; add.textContent = item?.selected ? "Selected" : "Select chart"; }
    const undo = $("#studio-undo");
    if (undo) undo.disabled = blocked || !context.undo?.available || context.undo.revision !== context.selection_revision;
    const preview = $("#studio-player-toggle");
    if (preview) {
      preview.disabled = state.loading || state.busy || (item && !spotifyId(item));
      preview.textContent = state.preview ? "Close Spotify player" : "Preview original song";
      preview.setAttribute("aria-expanded", String(state.preview));
    }
    panel.setAttribute("aria-busy", String(state.busy || state.loading));
    requestSelectionPreview();
  }

  function render() {
    const item = current();
    const difficulty = item ? bridge.difficulty({difficulty_coverage: item.difficulty_coverage}) : null;
    $("#studio-now-title").textContent = item ? `${item.title}${difficulty?.suffix || ""}` : "Your next eligible song";
    $("#studio-now-artist").textContent = item?.artist || "Shuffle or browse every eligible result";
    const coverage = $("#studio-now-coverage");
    if (coverage) {
      coverage.textContent = item ? difficulty?.text || "Difficulty levels unknown" : "Charts with actual notes are checked before ZIP inclusion";
    }
    const tier = $("#studio-now-tier");
    if (tier) tier.textContent = item ? ({RECOMMENDED_EXACT: "Recommended recording", SAME_RECORDING_CHART_VARIANT: "Same recording", ALTERNATE_RECORDING: "Different recording — review", POSSIBLE_MATCH: "Possible match — review"}[item.result_tier] || "Review this chart") : "";
    const chart = $("#studio-chart-title");
    if (chart) {
      chart.textContent = item?.chart_title ? `Chart: ${item.chart_artist || item.artist} — ${item.chart_title}` : "";
      chart.hidden = !item?.chart_title;
    }
    const link = $("#studio-now-link");
    const id = spotifyId(item);
    if (link) {
      link.hidden = !id;
      if (id) link.href = `https://open.spotify.com/track/${id}`;
      else link.removeAttribute("href");
      link.textContent = "Original song on Spotify";
    }
    const deck = $("#studio-deck");
    $("#studio-queue-position").textContent = item ? `Song ${formatCount(state.cursor + 1)} of ${formatCount(state.total)}${state.seed ? " · Shuffled order" : ""} · 100 load per page` : "Browse all eligible songs; 100 load per page.";
    if (deck) {
      deck.dataset.selected = String(Boolean(item?.selected));
      deck.dataset.scope = item?.scope || "";
    }
    if (state.preview && state.playerId !== id) closePlayer();
    syncControls();
  }

  function closePlayer() {
    state.preview = false;
    state.playerId = null;
    const player = $("#studio-player-host");
    player?.replaceChildren();
    if (player) player.hidden = true;
    syncControls();
  }

  function openPlayer() {
    const item = current();
    const id = spotifyId(item);
    if (!id) {
      status("No confirmed Spotify recording is available for this chart. You can still select the chart.");
      return;
    }
    const host = $("#studio-player-host");
    if (!host) return;
    host.replaceChildren();
    const iframe = document.createElement("iframe");
    iframe.src = `https://open.spotify.com/embed/track/${id}?theme=0`;
    iframe.title = `Original song on Spotify: ${item.artist} — ${item.title}`;
    iframe.width = "100%";
    iframe.height = "152";
    iframe.loading = "eager";
    iframe.allow = "encrypted-media; fullscreen; picture-in-picture";
    iframe.referrerPolicy = "strict-origin-when-cross-origin";
    iframe.addEventListener("error", () => status("Spotify could not load here. Use Original song on Spotify to listen in Spotify.", true));
    host.append(iframe);
    host.hidden = false;
    state.preview = true;
    state.playerId = id;
    syncControls();
    status("This is the original Spotify song. Use Spotify’s Play control; playback depends on your account and region. The chart recording may differ.");
  }

  function validatePage(data, expectedOffset) {
    if (!Array.isArray(data?.items) || data.items.length > PAGE_SIZE || !Number.isSafeInteger(data.total) || data.total < 0
        || data.offset !== expectedOffset || data.limit !== PAGE_SIZE || typeof data.has_more !== "boolean"
        || (state.loaded && data.total !== state.total)
        || typeof data.generation_token !== "string" || !data.generation_token) throw new Error("This listening queue could not be confirmed. Refresh your chart results and try again.");
    if (state.token && data.generation_token !== state.token) throw new Error("Your chart results changed. Start browsing again to use the new results.");
    return data.items.filter(item => item && typeof item.key === "string" && ["library", "expansion"].includes(item.scope)
      && Number.isSafeInteger(item.candidate_id) && item.candidate_id > 0 && item.selectable === true);
  }

  async function loadQueue(more = false) {
    if (state.loaded && (!more || state.complete)) return;
    if (state.request) return state.request;
    const epoch = state.epoch;
    const selectedScope = scope();
    const controller = new AbortController();
    state.controller = controller;
    state.loading = true;
    syncControls();
    const work = async () => {
      const offset = state.loaded ? state.items.length : 0;
      const params = new URLSearchParams({scope: selectedScope, limit: String(PAGE_SIZE), offset: String(offset)});
      if (state.token) params.set("generation_token", state.token);
      if (state.seed) { params.set("order", "shuffle"); params.set("seed", state.seed); }
      status(state.loaded ? "Loading the next songs..." : "Loading eligible songs for your listening queue...");
      const data = await bridge.api(`/api/listening-deck?${params}`, {signal: controller.signal});
      if (epoch !== state.epoch) return;
      const items = validatePage(data, offset);
      if (!state.loaded) {
        state.token = data.generation_token;
        state.total = data.total;
        state.items = items;
        state.cursor = -1;
        state.loaded = true;
      } else {
        const existing = new Set(state.items.map(item => item.key));
        if (items.some(item => existing.has(item.key))) throw new Error("The queue returned a repeated song. Refresh the results before continuing.");
        state.items.push(...items);
      }
      state.order = state.items.map((_, index) => index);
      state.complete = !data.has_more;
      if (!state.complete && items.length !== PAGE_SIZE) throw new Error("The next queue page could not be confirmed. Refresh the results before continuing.");
      if (state.complete && state.items.length !== state.total) throw new Error("The complete listening queue could not be confirmed. Refresh the results before continuing.");
      render();
    };
    state.request = work();
    try { await state.request; }
    catch (error) {
      if (epoch !== state.epoch || error.name === "AbortError") return;
      if (error.status === 409) reset("Your chart results changed. Start browsing again to use the new results.");
      else status(error.message || "The listening queue could not be loaded.", true);
      throw error;
    } finally {
      if (epoch === state.epoch) { state.request = null; state.loading = false; state.controller = null; syncControls(); }
    }
  }

  async function advanceQueue() {
    // Bound automatic skipping, so a mostly-added 50k-song library does not
    // launch hundreds of page requests from one tap. Next continues where it stopped.
    let pages = 0;
    while (pages < 3) {
      while (state.cursor + 1 < state.order.length) {
        state.cursor += 1;
        if (!skipped(current())) return true;
      }
      if (state.complete) {
        const eligible = state.items.findIndex(item => !skipped(item));
        if (eligible < 0) {
          status("Every song in this queue is already selected. Turn off Skip selected songs to browse them.");
          return false;
        }
        // A cycle only restarts after every song in the complete queue was visited.
        state.cursor = eligible;
        return true;
      }
      await loadQueue(true);
      pages += 1;
    }
    status("Already-added songs were skipped. Tap Next to continue through the rest of the queue.");
    return false;
  }

  async function nextSong({shuffle = false} = {}) {
    if (state.loading || state.busy || bridge.context().busy) return;
    try {
      const reopen = state.preview;
      if (shuffle) {
        reset("Shuffling every eligible song, across every results page...");
        const bytes = new Uint8Array(8);
        window.crypto.getRandomValues(bytes);
        state.seed = [...bytes].map(value => value.toString(16).padStart(2, "0")).join("");
      }
      const epoch = state.epoch;
      await loadQueue();
      if (epoch !== state.epoch || !state.items.length) {
        if (epoch === state.epoch) status("No eligible songs are available in this scope. Run Find or artist expansion first.");
        return;
      }
      const advanced = await advanceQueue();
      if (epoch !== state.epoch) return;
      render();
      if (reopen && spotifyId(current())) openPlayer();
      if (advanced) status(`${formatCount(state.cursor + 1)} of ${formatCount(state.total)} in the ${state.seed ? "shuffled " : ""}queue. ${current()?.scope === "expansion" ? "Artist expansion: no confirmed Spotify recording." : "Preview the original song or select its chart."}`);
    } catch (_) { /* loadQueue keeps the actionable message. */ }
  }

  async function selectAll() {
    const preview = state.operationPreview;
    if (!preview || state.previewKey !== previewKey()) return;
    if (state.busy || !bridge.begin()) return;
    const epoch = state.epoch;
    const selectedScope = scope();
    const policy = $("#studio-select-policy")?.value === "all_versions" ? "all_versions" : "one_per_song";
    try {
      state.busy = true;
      syncControls();
      $("#studio-select-status").textContent = "Adding songs across every page...";
      const saved = await bridge.api("/api/selection/all", {method: "POST", body: JSON.stringify({scope: selectedScope, policy, confirm_possible: $("#studio-include-uncertain").checked, selection_revision: preview.selection_revision, exact_generation_id: preview.exact_generation_id})});
      if (epoch !== state.epoch) return;
      const confirmed = bridge.apply(saved) || await bridge.reconcile("All-page choices saved.");
      if (!confirmed) throw new Error("Choices were saved, but the updated totals are not confirmed. Refresh the selection summary before downloading.");
      const message = `${formatCount(saved.songs)} songs / ${formatCount(saved.charts)} charts in your selection tray. All pages included.`;
      $("#studio-select-status").textContent = message;
      bridge.announce(message);
      render();
    } catch (error) {
      const reconciled = await bridge.reconcile("The all-page save result is not confirmed.");
      if (!reconciled) bridge.stale("The all-page save result is not confirmed.");
      $("#studio-select-status").textContent = error.message || "All-page selection could not be saved.";
    } finally { state.busy = false; bridge.finish(); syncControls(); }
  }

  async function changeCurrent() {
    const target = current();
    if (!target || target.selected || state.busy || !bridge.begin()) return;
    const epoch = state.epoch;
    try {
      if (target.result_tier === "POSSIBLE_MATCH" && !window.confirm("This chart is a possible match with uncertain song identity. Review its title and artist. Select this chart?")) return;
      state.busy = true;
      syncControls();
      const payload = {...bridge.guard(), candidate_id: target.candidate_id, action: "select", confirm_possible: target.result_tier === "POSSIBLE_MATCH"};
      if (target.scope === "expansion") Object.assign(payload, {kind: "expansion", artist_norm: target.artist_norm});
      else payload.track_key = target.track_key;
      const saved = await bridge.api("/api/selection", {method: "POST", body: JSON.stringify(payload)});
      if (epoch !== state.epoch) return;
      const confirmed = bridge.apply(saved) || await bridge.reconcile("Chart choice saved.");
      if (!confirmed) throw new Error("The updated selection tray could not be confirmed. Refresh the selection summary before downloading.");
      const message = `Added ${target.title} to your selection tray. Undo last selection change restores the previous choices.`;
      status(message);
      bridge.announce(message);
      closePlayer();
      await advanceQueue();
      if (epoch !== state.epoch) return;
      render();
    } catch (error) {
      if (epoch !== state.epoch) return;
      const reconciled = await bridge.reconcile("The chart save result is not confirmed.");
      if (!reconciled) bridge.stale("The chart save result is not confirmed.");
      status(error.message || "This chart choice could not be saved.", true);
    } finally { state.busy = false; bridge.finish(); syncControls(); }
  }

  function previewKey() {
    const context = bridge.context();
    return [context.workspace, context.generation, context.selection_revision, scope(), $("#studio-select-policy")?.value, $("#studio-include-uncertain")?.checked].join(":");
  }

  function requestSelectionPreview(force = false) {
    const context = bridge.context();
    if (context.stage !== "confirm" || context.busy || context.stale || !context.known) return;
    const key = previewKey();
    if (!force && state.previewKey === key) return;
    state.previewKey = key;
    state.operationPreview = null;
    const requestId = ++state.previewRequest;
    const host = $("#studio-selection-preview");
    host.textContent = "Checking the full operation across all pages…";
    $("#studio-select-all").disabled = true;
    const params = new URLSearchParams({scope: scope(), policy: $("#studio-select-policy").value, include_possible: String($("#studio-include-uncertain").checked)});
    bridge.api(`/api/selection/all-preview?${params}`, {timeoutMs: 8000}).then(data => {
      if (requestId !== state.previewRequest || key !== previewKey()) return;
      if (![data.songs, data.charts, data.possible_charts, data.selection_revision].every(value => Number.isSafeInteger(value) && value >= 0)
          || data.scope !== scope() || data.policy !== $("#studio-select-policy").value || data.include_possible !== $("#studio-include-uncertain").checked) throw new Error("The operation preview was not confirmed.");
      state.operationPreview = data;
      const scopeLabel = data.scope === "both" ? "your imported music and artist expansion" : data.scope === "library" ? "your imported music" : "artist expansion";
      const restoreNote = data.undo_supported === true ? "Undo restores the previous choices." : data.undo_supported === false ? "The current selection is too large to keep for undo. Applying this change replaces its choices without an undo receipt." : "Undo availability will be confirmed after saving.";
      host.textContent = `Replace choices in ${scopeLabel} with ${formatCount(data.charts)} chart entries for ${formatCount(data.songs)} songs across all pages. ${data.policy === "one_per_song" ? "One preferred chart per song." : "Every eligible version."} ${data.include_possible ? `${formatCount(data.possible_charts)} possible-match charts included; review their identity.` : "Possible matches excluded."} Choices outside this scope stay saved. ${restoreNote}`;
      syncControls();
    }).catch(() => {
      if (requestId !== state.previewRequest || key !== previewKey()) return;
      state.operationPreview = null;
      host.textContent = "Selection preview unavailable. Check the operation before applying it. ";
      const button = document.createElement("button");
      button.type = "button"; button.className = "text-button"; button.textContent = "Retry preview";
      button.addEventListener("click", () => requestSelectionPreview(true));
      host.append(button);
      $("#studio-select-all").disabled = true;
    });
  }

  $("#studio-select-all")?.addEventListener("click", selectAll);
  $("#studio-select-scope")?.addEventListener("change", () => reset("Scope changed. Shuffle or browse this set of eligible songs."));
  $("#studio-select-policy")?.addEventListener("change", () => requestSelectionPreview());
  $("#studio-include-uncertain")?.addEventListener("change", () => requestSelectionPreview());
  $("#studio-next")?.addEventListener("click", () => nextSong());
  $("#studio-shuffle")?.addEventListener("click", () => nextSong({shuffle: true}));
  $("#studio-add")?.addEventListener("click", () => changeCurrent());
  $("#studio-undo")?.addEventListener("click", async () => {
    const restored = await bridge.undo();
    if (restored) status("The last selection change was undone. Previous customized choices are restored.");
    render();
  });
  $("#studio-skip-selected")?.addEventListener("change", () => { render(); });
  $("#studio-player-toggle")?.addEventListener("click", async () => {
    if (state.preview) { closePlayer(); return; }
    try {
      await loadQueue();
      if (state.cursor < 0) await advanceQueue();
      render();
      openPlayer();
    } catch (_) { /* Queue message is already visible. */ }
  });
  document.addEventListener("riff:studio-reset", () => reset());
  document.addEventListener("riff:studio-context", event => {
    const context = event.detail || bridge.context();
    const key = `${context.workspace ?? ""}:${context.generation || ""}:${$("#expansionResultCount")?.textContent || ""}`;
    if (state.loaded && state.contextKey && state.contextKey !== key) reset("Your chart results changed. Shuffle or browse the current results.");
    state.contextKey = key;
    const allStatus = $("#studio-select-status");
    if (allStatus && context.known && /Loading your chart pool/.test(allStatus.textContent)) allStatus.textContent = "Ready · selections include every page.";
    if (context.stage !== "confirm") closePlayer();
    syncControls();
  });
  document.addEventListener("riff:selection-state", syncControls);
  document.addEventListener("riff:selection-committed", event => {
    const selected = event.detail?.selected;
    if (!selected) return;
    state.selectionSerial += 1;
    const library = new Set(selected.library.map(row => `${row.track_key}:${row.candidate_id}`));
    const librarySongs = new Set(selected.library.map(row => row.track_key));
    const expansion = new Set(selected.expansion.map(row => Number(row.candidate_id)));
    const expansionSongs = new Set(selected.expansion.filter(row => typeof row.song_key === "string").map(row => row.song_key));
    state.items.forEach(item => {
      item.selected = item.scope === "expansion" ? expansion.has(item.candidate_id) : library.has(`${item.track_key}:${item.candidate_id}`);
      item.song_selected = item.scope === "expansion"
        ? expansionSongs.has(item.song_key || item.key.slice("expansion:".length)) || item.selected
        : librarySongs.has(item.track_key);
    });
    render();
  });
  document.addEventListener("keydown", event => {
    if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || event.repeat
        || bridge.context().stage !== "confirm" || !panel.contains(document.activeElement)
        || event.target.closest("input, textarea, select, button, a, [contenteditable=true]")) return;
    if (event.key === "ArrowRight") { event.preventDefault(); void nextSong(); }
    else if (event.key.toLowerCase() === "a") { event.preventDefault(); void changeCurrent(); }
  });
  window.addEventListener("pagehide", () => reset());
  render();
})()
