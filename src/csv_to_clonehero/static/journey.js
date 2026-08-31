(function () {
  "use strict";

  const app = document.getElementById("app");
  const announcer = document.getElementById("announcer");
  const model = {
    snapshot: null,
    overlay: null,
    pendingAction: "",
    requestRevision: 0,
    pollTimer: null,
    elapsedTimer: null,
    elapsedSeconds: 0,
    elapsedStartedAt: 0,
    elapsedReceivedAt: 0,
    controller: null,
    opener: null,
    formatOpen: false,
    spotifyScope: null
  };

  const icons = {
    arrow: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h13M13 6l6 6-6 6"/></svg>',
    check: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6"/></svg>',
    close: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg>',
    download: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v12m0 0 5-5m-5 5-5-5M5 20h14"/></svg>',
    sparkle: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 3 1.6 5.4L19 10l-5.4 1.6L12 17l-1.6-5.4L5 10l5.4-1.6L12 3Z"/></svg>',
    vinyl: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="2"/><path d="M12 3.5v3M20.5 12h-3M12 20.5v-3M3.5 12h3"/></svg>'
  };

  const sourceInfo = {
    spotify: { description: "Liked songs and saved albums", art: "source-spotify", mark: "S" },
    soundcloud: { description: "Likes and playlists", art: "source-soundcloud", mark: "sc" },
    apple: { description: "Your library, in one place", art: "source-apple", mark: "♪" },
    youtube: { description: "Likes and saved mixes", art: "source-youtube", mark: "▶" },
    tidal: { description: "Albums and favorites", art: "source-tidal", mark: "T" },
    local: { description: "A folder on this device", art: "source-local", mark: "↗" }
  };

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>'"]/g, function (character) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[character];
    });
  }

  function number(value) { return Number(value || 0).toLocaleString("en-US"); }

  function elapsed(value) {
    const seconds = Math.max(0, Math.round(Number(value || 0)));
    const minutes = Math.floor(seconds / 60);
    return minutes ? minutes + "m " + (seconds % 60) + "s" : seconds + "s";
  }

  function matchingActive(snapshot) {
    const state = snapshot && snapshot.matching && snapshot.matching.state;
    return Boolean(snapshot && (
      snapshot.operation && snapshot.operation.state === "importing" ||
      state === "matching_restoring" || state === "matching_running"
    ));
  }

  function stopPolling() {
    if (model.pollTimer !== null) {
      window.clearTimeout(model.pollTimer);
      model.pollTimer = null;
    }
  }

  function stopElapsedTicker() {
    if (model.elapsedTimer !== null) {
      window.clearTimeout(model.elapsedTimer);
      model.elapsedTimer = null;
    }
  }

  function monotonicNow() {
    return window.performance && typeof window.performance.now === "function"
      ? window.performance.now() : Date.now();
  }

  function reconcileElapsed(snapshot) {
    const matching = snapshot && snapshot.matching;
    const startedAt = Number(matching && matching.started_at || 0);
    model.elapsedStartedAt = startedAt;
    model.elapsedSeconds = Math.max(0, Number(matching && matching.elapsed_seconds || 0));
    model.elapsedReceivedAt = monotonicNow();
  }

  function displayedElapsed(matching) {
    const serverValue = Math.max(0, Number(matching && matching.elapsed_seconds || 0));
    const now = monotonicNow();
    const localValue = model.elapsedReceivedAt
      ? model.elapsedSeconds + Math.max(0, now - model.elapsedReceivedAt) / 1000
      : serverValue;
    const startedValue = Number(matching && matching.started_at || model.elapsedStartedAt || 0);
    const wallValue = startedValue ? Math.max(0, Date.now() / 1000 - startedValue) : 0;
    return Math.max(serverValue, localValue, wallValue);
  }

  function updateElapsedDisplay() {
    const matching = model.snapshot && model.snapshot.matching;
    const element = document.getElementById("matching-elapsed");
    if (element && matching) element.textContent = elapsed(displayedElapsed(matching));
    if (matchingActive(model.snapshot)) {
      model.elapsedTimer = window.setTimeout(updateElapsedDisplay, 1000);
    } else {
      model.elapsedTimer = null;
    }
  }

  function scheduleElapsedTicker(snapshot) {
    stopElapsedTicker();
    if (matchingActive(snapshot) && snapshot.matching && snapshot.matching.state === "matching_running") {
      model.elapsedTimer = window.setTimeout(updateElapsedDisplay, 1000);
    }
  }

  function schedulePolling(snapshot) {
    stopPolling();
    if (matchingActive(snapshot)) model.pollTimer = window.setTimeout(refresh, 850);
  }

  function announce(message) { announcer.textContent = message; }

  function apiUrl(path) {
    const current = new URLSearchParams(window.location.search);
    const token = current.get("access_token");
    if (!token) return path;
    return path + (path.includes("?") ? "&" : "?") + "access_token=" + encodeURIComponent(token);
  }

  async function api(path, options) {
    const response = await fetch(apiUrl(path), Object.assign({ credentials: "same-origin" }, options || {}));
    const payload = await response.json().catch(function () { return {}; });
    if (!response.ok) throw new Error(payload.error || "The request could not be completed.");
    return payload;
  }

  function sourceMark(source) {
    const info = sourceInfo[source.source_key] || {};
    return '<span class="source-mark ' + escapeHtml(info.art) + '" aria-hidden="true"><span>' + escapeHtml(info.mark) + '</span></span>';
  }

  function statusCue(source) {
    const labels = {
      not_added: "Not added", working: "Updating", added: "Imported",
      attention: source.attention_reason || "Needs attention", coming_soon: "Coming soon"
    };
    return '<span class="status-cue status-' + escapeHtml(source.state) + '"><span class="status-symbol" aria-hidden="true"></span><span>' + escapeHtml(labels[source.state] || source.state) + '</span></span>';
  }

  function sourceTile(source, compact) {
    const info = sourceInfo[source.source_key] || {};
    const detail = source.song_count ? number(source.song_count) + " songs imported" : info.description;
    const copy = sourceMark(source) + '<span class="source-tile-copy"><strong>' + escapeHtml(source.label) + '</strong><small>' + escapeHtml(detail) + '</small>' + statusCue(source) + '</span>';
    if (source.availability === "coming_soon") {
      return '<article class="source-tile source-coming' + (compact ? " compact" : "") + '" aria-label="' + escapeHtml(source.label) + ', coming soon">' + copy + '</article>';
    }
    return '<button class="source-tile source-action' + (compact ? " compact" : "") + '" data-action="open-source" data-source="' + escapeHtml(source.source_key) + '" aria-label="Open ' + escapeHtml(source.label) + ' source">' + copy + icons.arrow + '</button>';
  }

  function shell(content, stage) {
    document.getElementById("stage-label").textContent = stage;
    return '<main id="main-content" class="main-content" tabindex="-1">' + content + '</main>';
  }

  function sourcesEmpty(snapshot) {
    const sources = snapshot.sources;
    return shell('<section class="source-stage stage-section"><div class="source-intro"><p class="overline">Add music</p><h1>Start with the music you already love.</h1><p class="lead-copy">Bring your listening history into one library, then find the charts that fit it.</p><div class="journey-note"><span class="journey-note-mark">' + icons.sparkle + '</span><span><strong>One simple path</strong><small>Add music. Choose charts. Play.</small></span></div></div><section class="source-drawer" aria-labelledby="source-drawer-heading"><header class="drawer-head"><div><p class="overline">Your music sources</p><h2 id="source-drawer-heading">Add a source</h2></div><span class="drawer-meta">Ready when you are</span></header><div class="source-grid source-grid-supported">' + sourceTile(sources.spotify) + sourceTile(sources.soundcloud) + '</div><div class="future-heading"><span>On the way</span><span class="future-rule"></span><small>Coming soon</small></div><div class="source-grid source-grid-future">' + sourceTile(sources.apple) + sourceTile(sources.youtube) + sourceTile(sources.tidal) + sourceTile(sources.local) + '</div></section></section>', "Add music");
  }

  function sourceSummary(source, count) {
    const info = sourceInfo[source.source_key] || {};
    return '<div class="source-summary-row"><span class="source-summary-name"><span class="mini-source ' + escapeHtml(info.art) + '" aria-hidden="true">' + escapeHtml(info.mark) + '</span>' + escapeHtml(source.label) + '</span><strong>' + number(count) + '</strong><span>songs</span></div>';
  }

  function musicReady(snapshot) {
    const music = snapshot.music;
    const spotify = snapshot.sources.spotify;
    const soundcloud = snapshot.sources.soundcloud;
    const attention = snapshot.operation.state === "attention" ? '<div class="attention-banner" role="status"><span class="attention-symbol">!</span><span><strong>SoundCloud update needs attention</strong><small>Your previous imported music is still here.</small></span></div>' : "";
    const canFind = Boolean(snapshot.allowed_actions && snapshot.allowed_actions.find_charts);
    return shell('<section class="ready-stage stage-section" data-test="music-ready"><div class="ready-copy"><p class="overline">Music ready</p><h1><span class="hero-number">' + number(music.canonical_count) + '</span><span class="hero-unit">songs</span></h1><p class="lead-copy">' + number(music.canonical_count) + ' unique songs after duplicates were removed.</p>' + attention + '<div class="source-summary" aria-label="Music source summary">' + sourceSummary(spotify, music.spotify_count) + sourceSummary(soundcloud, music.soundcloud_count) + '<div class="source-summary-row shared-row"><span class="source-summary-name"><span class="shared-mark" aria-hidden="true">+</span>In both</span><strong>' + number(music.shared_count) + '</strong><span>songs</span></div></div><div class="ready-actions"><div class="button-row"><button class="button primary" data-test="find-charts" data-action="find-charts"' + (canFind ? "" : ' disabled aria-disabled="true"') + '>Find Clone Hero charts ' + icons.arrow + '</button><div class="format-action"><button class="button secondary" data-action="toggle-format" aria-haspopup="dialog" aria-expanded="' + (model.formatOpen ? "true" : "false") + '">' + icons.download + ' Download my song list</button>' + (model.formatOpen ? formatChooser() : "") + '</div></div><button class="text-link" data-action="manage-sources">Manage music sources ' + icons.arrow + '</button></div></div><div class="ready-visual" aria-hidden="true"><div class="source-orbit orbit-spotify"><span>Spotify</span><strong>' + number(music.spotify_count) + '</strong></div><div class="source-orbit orbit-soundcloud"><span>SoundCloud</span><strong>' + number(music.soundcloud_count) + '</strong></div><div class="orbit-center">' + icons.vinyl + '<span>in one<br>place</span></div><div class="visual-spark spark-one"></div><div class="visual-spark spark-two"></div></div></section>', "Music ready");
  }

  function importing(snapshot) {
    const operation = snapshot.operation || {};
    return shell('<section class="loading-stage stage-section" data-test="importing"><p class="overline">Your music</p><h1>Restoring your library...</h1><p class="matching-status-line">' + escapeHtml(operation.message || "Adding your music...") + '</p><p class="lead-copy">Your imported music will be ready when this finishes.</p></section>', "Add music");
  }

  function matchingDetails(matching) {
    const expectation = matching.details && matching.details.large_library_expectation;
    const reuse = matching.details && matching.details.saved_work_reuse;
    return '<details class="details"><summary>Details</summary><p>' +
      (reuse ? "Saved chart work was reused without checking every song again. " : "") +
      "Results are shown only when the current check is complete. " + escapeHtml(expectation || "") +
      '</p></details>';
  }

  function matchingVisual(snapshot) {
    const matching = snapshot.matching;
    const state = matching.state;
    const total = Number(matching.total || 0);
    const processed = Number(matching.processed || 0);
    const boundedProcessed = total > 0 ? Math.min(total, Math.max(0, processed)) : 0;
    const determinate = state === "matching_running" && total > 0;
    const percent = determinate ? Math.round((boundedProcessed / total) * 100) : 0;
    const ring = '<div class="progress-ring ' + (determinate ? "" : "is-indeterminate") + '"' + (determinate ? ' style="--progress:' + percent + '%" role="progressbar" aria-label="Songs checked" aria-valuemin="0" aria-valuemax="' + total + '" aria-valuenow="' + boundedProcessed + '"' : ' role="progressbar" aria-label="Checking saved match results"') + '><div class="progress-ring-inner"><strong>' + (determinate ? percent + "%" : "") + '</strong><span>' + (determinate ? "checked" : "checking") + '</span></div></div>';
    let copy;
    if (state === "matching_restoring") {
      copy = '<p class="overline">Find charts</p><h1>Finding your Clone Hero charts</h1><p class="matching-status-line">Checking saved match results...</p><p class="lead-copy">Your saved work is being checked before anything new runs.</p>' + matchingDetails(matching);
    } else if (state === "matching_running") {
      const current = matching.current_song;
      copy = '<p class="overline">Find charts</p><h1>Finding your Clone Hero charts</h1><p class="matching-count"><strong>' + number(boundedProcessed) + '</strong> / ' + number(total) + '</p><div class="fine-progress' + (determinate ? "" : " is-indeterminate") + '" aria-hidden="true"><span style="width:' + percent + '%"></span></div>' + (current ? '<div class="current-check"><div class="current-art" aria-hidden="true"></div><div><small>Currently checking</small><strong>' + escapeHtml(current.artist) + ' <span>—</span> ' + escapeHtml(current.title) + '</strong></div></div>' : '<p class="matching-preparing">Preparing your songs...</p>') + '<p class="elapsed"><span>Elapsed</span><strong id="matching-elapsed">' + elapsed(displayedElapsed(matching)) + '</strong></p><div class="matching-reassurance"><span class="reassurance-mark">' + icons.check + '</span><span><strong>No action needed.</strong><small>Keep the app running; you can leave this screen.</small></span></div>' + matchingDetails(matching);
    } else if (state === "matching_interrupted") {
      copy = '<p class="overline">Find charts</p><h1>Matching stopped before it finished</h1><p class="matching-count"><strong>' + number(processed) + '</strong> of ' + number(total) + ' songs checked</p><p class="lead-copy">Your completed saved work can be reused when you try again.</p><button class="button primary" data-test="retry-matching" data-action="retry-matching">Try Find Charts again ' + icons.arrow + '</button>' + matchingDetails(matching);
    } else {
      copy = '<p class="overline">Find charts</p><h1>We couldn’t finish finding charts</h1><p class="lead-copy">Your music is safe.</p><button class="button primary" data-test="retry-matching" data-action="retry-matching">Try Find Charts again ' + icons.arrow + '</button>' + matchingDetails(matching);
    }
    return shell('<section class="matching-stage stage-section" data-test="' + escapeHtml(state) + '"><div class="matching-visual"><div class="matching-glow"></div>' + ring + '<div class="matching-record" aria-hidden="true"></div></div><div class="matching-copy">' + copy + '</div></section>', "Find charts");
  }

  function cohortCard(count, title, description) {
    return '<article class="cohort-card"><div class="cohort-card-head"><div class="cohort-index" aria-hidden="true"></div><div><p class="cohort-label">Recommended</p><h2><strong>' + number(count) + '</strong> ' + title + '</h2></div></div><p>' + description + '</p><button class="cohort-action" data-test="cohort-placeholder" data-action="cohort-placeholder" disabled aria-disabled="true">Available in the next step' + icons.arrow + '</button></article>';
  }

  function chartsFound(snapshot) {
    const result = snapshot.matching.results || {};
    const details = '<details class="details"><summary>Details</summary><p>' + number(result.no_suitable) + ' no suitable, ' + number(result.not_playable) + ' not playable, and ' + number(result.no_candidate) + ' had no candidate.</p></details>';
    return shell('<section class="found-stage stage-section" data-test="charts-found"><header class="found-head"><div><p class="overline">Charts found</p><h1>We found charts for <span>' + number(result.recommendations) + '</span> of your songs.</h1></div><div class="selection-status"><span class="empty-status-mark" aria-hidden="true"></span><span>Nothing selected yet</span></div></header><div class="cohort-grid">' + cohortCard(result.best_matches, "Best matches", "Most reliable recommendations for artist, title, version, and playability.") + cohortCard(result.best_available, "Best available", "Other playable recommendations that may need a closer look.") + '</div><footer class="found-footer"><div class="found-gaps"><span><strong>' + number(result.no_suitable) + '</strong> no suitable</span><span><strong>' + number(result.not_playable) + '</strong> not playable</span><span><strong>' + number(result.no_candidate) + '</strong> no candidate</span></div>' + details + '</footer></section>', "Confirm charts");
  }

  function formatChooser() {
    return '<div class="format-popover" role="dialog" aria-modal="true" aria-labelledby="format-heading"><div class="format-popover-head"><div><p class="overline">Song list</p><h2 id="format-heading">Choose a format</h2></div><button class="icon-button" data-action="close-format" aria-label="Close format chooser">' + icons.close + '</button></div><div class="format-options"><button class="format-option" data-action="choose-format" data-format="txt"><span class="format-type">TXT</span><span><strong>Artist - Song</strong><small>Plain text, easy to read</small></span>' + icons.arrow + '</button><button class="format-option" data-action="choose-format" data-format="csv"><span class="format-type">CSV</span><span><strong>Structured music list</strong><small>One song per row</small></span>' + icons.arrow + '</button><button class="format-option" data-action="choose-format" data-format="json"><span class="format-type">JSON</span><span><strong>Structured list with sources</strong><small>Includes source information</small></span>' + icons.arrow + '</button></div></div>';
  }

  function spotifyScopeChooser(scope) {
    const selected = scope.scope || {};
    const liked = Boolean(selected.include_liked);
    const likedCount = scope.liked_available && scope.liked_count != null
      ? " (" + number(scope.liked_count) + " songs)" : "";
    const playlists = (scope.playlists || []).map(function (playlist) {
      const accessible = playlist.access_status === "accessible";
      const checked = playlist.selected ? " checked" : "";
      return '<label class="scope-option"><input type="checkbox" data-spotify-playlist="' + escapeHtml(playlist.spotify_id) + '"' + checked + (accessible ? "" : " disabled") + '><span><strong>' + escapeHtml(playlist.name || "Untitled playlist") + '</strong><small>' + escapeHtml(accessible ? number(playlist.item_count || 0) + " songs" : (playlist.access_reason || "Unavailable")) + '</small></span></label>';
    }).join("");
    return '<section class="spotify-scope" aria-labelledby="spotify-scope-heading"><p class="overline">Choose music</p><h3 id="spotify-scope-heading">Add from Spotify</h3><label class="scope-option"><input type="checkbox" data-spotify-liked' + (liked ? " checked" : "") + (scope.liked_available === false && !liked ? " disabled" : "") + '><span><strong>Liked Songs</strong><small>' + escapeHtml(scope.liked_available === false ? "Count unavailable" : (likedCount || "Saved songs")) + '</small></span></label>' + (playlists || '<p class="scope-empty">No accessible playlists are available yet.</p>') + '<button class="button primary" data-action="save-spotify-scope">Add selected Spotify music ' + icons.arrow + '</button></section>';
  }

  function sourceSheet(source) {
    const info = sourceInfo[source.source_key] || {};
    const imported = Boolean(source.imported);
    const spotify = source.source_key === "spotify";
    const soundcloud = source.source_key === "soundcloud";
    let body = "";
    if (imported) {
      const date = source.updated_at ? new Date(source.updated_at).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }) : "recently";
      const spotifyActions = source.connected ? '<button class="button primary" data-action="update-spotify">Update music</button><button class="button secondary" data-action="reconnect-source">Reconnect</button>' : '<button class="button primary" data-action="reconnect-source">Reconnect to update</button>';
      body = '<p class="sheet-lead">Your imported music is part of this library.</p><div class="sheet-stat"><strong>' + number(source.song_count) + '</strong><span>songs imported</span></div><p class="sheet-updated">Last updated ' + escapeHtml(date) + '</p>' + (source.state === "attention" ? '<div class="attention-note"><span class="attention-symbol">!</span><span><strong>Reconnect to update</strong><small>Your imported songs are still here.</small></span></div>' : "") + (soundcloud ? '<label class="file-picker"><span>Choose a replacement export</span><input id="soundcloud-file" type="file" accept=".csv,.json,text/csv,application/json"></label>' : "") + '<div class="sheet-actions">' + (spotify ? spotifyActions : '<button class="button primary" data-action="import-soundcloud">Replace import</button>') + '<button class="button quiet" data-action="view-imported">View imported music</button></div>' + (spotify && model.spotifyScope ? spotifyScopeChooser(model.spotifyScope) : "");
    } else if (spotify) {
      body = '<p class="sheet-lead">Bring your listening history into this music library.</p><div class="sheet-preview"><span class="preview-mark">' + icons.sparkle + '</span><div><strong>Ready to bring it in</strong><small>Liked songs and saved albums stay in your library.</small></div></div><div class="sheet-actions"><button class="button primary" data-action="' + (source.connected ? 'update-spotify' : 'reconnect-source') + '">' + (source.connected ? 'Add Spotify music' : 'Connect Spotify') + '</button></div>' + (source.connected && model.spotifyScope ? spotifyScopeChooser(model.spotifyScope) : "");
    } else {
      body = '<p class="sheet-lead">Bring this source into your music library.</p><div class="sheet-preview"><span class="preview-mark">' + icons.sparkle + '</span><div><strong>Import a CSV or JSON export</strong><small>Your existing music stays safe if a replacement fails.</small></div></div><label class="file-picker"><span>Choose SoundCloud export</span><input id="soundcloud-file" type="file" accept=".csv,.json,text/csv,application/json"></label><div class="sheet-actions"><button class="button primary" data-action="import-soundcloud">Import SoundCloud music</button></div>';
    }
    return '<div class="sheet-backdrop" data-action="backdrop"><aside class="source-sheet" role="dialog" aria-modal="true" aria-labelledby="source-sheet-heading"><header class="sheet-head"><div class="sheet-source-title"><span class="source-mark ' + escapeHtml(info.art) + '" aria-hidden="true"><span>' + escapeHtml(info.mark) + '</span></span><div><p class="overline">Music source</p><h2 id="source-sheet-heading">' + escapeHtml(source.label) + '</h2><div class="sheet-status">' + statusCue(source) + '</div></div></div><button class="icon-button" data-action="close-sheet" aria-label="Close source sheet">' + icons.close + '</button></header><div class="sheet-body">' + body + '</div></aside></div>';
  }

  function render(focusOverlay) {
    const snapshot = model.snapshot;
    if (!snapshot) return;
    let content;
    if (snapshot.matching && snapshot.matching.state === "charts_found") content = chartsFound(snapshot);
    else if (snapshot.matching && snapshot.matching.state !== "music_ready") content = matchingVisual(snapshot);
    else if (snapshot.operation && snapshot.operation.state === "importing") content = importing(snapshot);
    else content = snapshot.music.canonical_count ? musicReady(snapshot) : sourcesEmpty(snapshot);
    const stage = snapshot.matching && snapshot.matching.state === "charts_found" ? "Confirm charts" : snapshot.matching && snapshot.matching.state !== "music_ready" ? "Find charts" : snapshot.operation.state === "importing" ? "Add music" : snapshot.music.canonical_count ? "Music ready" : "Add music";
    app.innerHTML = '<header class="topbar"><div class="brand"><span class="brand-mark" aria-hidden="true"><i></i><i></i><i></i></span><span class="brand-name">Riff</span><span class="brand-divider" aria-hidden="true"></span><span class="brand-context">music to Clone Hero</span></div><div class="stage-label"><span class="stage-pip" aria-hidden="true"></span><span id="stage-label">' + stage + '</span></div></header>' + content.slice(content.indexOf("<main"));
    app.setAttribute("aria-busy", matchingActive(snapshot) ? "true" : "false");
    if (model.overlay) {
      app.insertAdjacentHTML("beforeend", sourceSheet(snapshot.sources[model.overlay]));
      if (focusOverlay) {
        const close = app.querySelector(".source-sheet [data-action='close-sheet']");
        if (close) close.focus();
      }
    }
    if (model.formatOpen && !model.overlay) {
      const close = app.querySelector(".format-popover [data-action='close-format']");
      if (focusOverlay && close) close.focus();
    }
  }

  function focusOpener(opener) {
    if (!opener) return;
    const candidates = Array.from(app.querySelectorAll("[data-action]"));
    const target = candidates.find(function (element) {
      return opener.action === element.dataset.action && (!opener.source || opener.source === element.dataset.source);
    });
    if (target) target.focus();
    model.opener = null;
  }

  function focusables(container) {
    return Array.from(container.querySelectorAll("button:not(:disabled), input:not(:disabled), [href], [tabindex]:not([tabindex='-1'])"));
  }

  async function refresh() {
    const revision = ++model.requestRevision;
    if (model.controller) model.controller.abort();
    model.controller = new AbortController();
    try {
      const response = await fetch(apiUrl("/api/journey"), { credentials: "same-origin", signal: model.controller.signal });
      const snapshot = await response.json();
      if (!response.ok) throw new Error(snapshot.error || "Your music could not be loaded.");
      if (revision !== model.requestRevision) return;
      const previous = model.snapshot;
      const wasImporting = previous && previous.operation && previous.operation.state === "importing";
      const wasMatching = previous && previous.matching && ["matching_restoring", "matching_running"].includes(previous.matching.state);
      reconcileElapsed(snapshot);
      model.snapshot = snapshot;
      render(false);
      if (wasImporting && snapshot.operation.state !== "importing") announce("Your music import finished.");
      if (wasMatching && !["matching_restoring", "matching_running"].includes(snapshot.matching && snapshot.matching.state)) announce("Chart finding finished.");
      schedulePolling(snapshot);
      scheduleElapsedTicker(snapshot);
    } catch (error) {
      if (error.name === "AbortError") return;
      if (revision !== model.requestRevision) return;
      stopPolling();
      stopElapsedTicker();
      announce(error.message);
      const main = document.getElementById("main-content");
      if (main) main.innerHTML = '<section class="error-stage stage-section" data-test="journey-error"><p class="overline">Your music</p><h1>We could not load your library.</h1><p class="lead-copy">' + escapeHtml(error.message) + '</p><button class="button secondary" data-action="retry">Retry</button></section>';
      app.setAttribute("aria-busy", "false");
    }
  }

  async function importSoundCloud() {
    const input = document.getElementById("soundcloud-file");
    if (!input || !input.files[0]) { announce("Choose a CSV or JSON SoundCloud export first."); return; }
    model.pendingAction = "import";
    const body = new FormData();
    body.append("file", input.files[0]);
    try {
      await api("/api/sources/soundcloud/import", { method: "POST", body: body });
      model.overlay = null;
      model.pendingAction = "";
      announce("SoundCloud import started.");
      await refresh();
    } catch (error) {
      model.pendingAction = "";
      announce(error.message);
    }
  }

  async function updateSpotify() {
    model.pendingAction = "spotify";
    try {
      const workspace = model.snapshot.workspace_id;
      let scope = await api("/api/spotify/scope?workspace_id=" + encodeURIComponent(workspace));
      const hasChoices = Boolean(scope.scope && (scope.scope.include_liked || (scope.scope.playlist_ids || []).length));
      if (!hasChoices && !(scope.playlists || []).length && scope.liked_available === false) {
        await api("/api/spotify/inventory", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ workspace_id: workspace }) });
        model.overlay = null;
        model.pendingAction = "";
        announce("Spotify sources are being updated.");
        await refresh();
        return;
      }
      if (!hasChoices) {
        model.spotifyScope = scope;
        render(true);
        announce("Choose the Spotify music to add.");
        return;
      }
      await api("/api/spotify/import", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ workspace_id: workspace }) });
      model.spotifyScope = null;
      model.overlay = null;
      announce("Spotify music update started.");
      await refresh();
    } catch (error) { announce(error.message); }
    model.pendingAction = "";
  }

  async function saveSpotifyScope() {
    if (!model.snapshot || model.pendingAction) return;
    const liked = document.querySelector("[data-spotify-liked]");
    const playlistIds = Array.from(document.querySelectorAll("[data-spotify-playlist]:checked")).map(function (input) { return input.dataset.spotifyPlaylist; });
    if (!liked || (!liked.checked && !playlistIds.length)) {
      announce("Choose Liked Songs or at least one playlist first.");
      return;
    }
    model.pendingAction = "spotify";
    try {
      const workspace = model.snapshot.workspace_id;
      await api("/api/spotify/scope", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ workspace_id: workspace, playlist_ids: playlistIds, include_liked: liked.checked }) });
      await api("/api/spotify/import", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ workspace_id: workspace }) });
      model.spotifyScope = null;
      model.overlay = null;
      announce("Spotify music import started.");
      await refresh();
    } catch (error) { announce(error.message); }
    model.pendingAction = "";
  }

  async function startMatching() {
    if (model.pendingAction) {
      announce("Chart finding is already starting.");
      return;
    }
    if (!model.snapshot || !model.snapshot.allowed_actions.find_charts) return;
    model.pendingAction = "matching";
    try {
      await api("/api/discovery/start", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ source: "combined", workspace_id: model.snapshot.workspace_id }) });
      announce("Chart finding started.");
      await refresh();
    } catch (error) {
      announce(error.message);
      await refresh();
    } finally { model.pendingAction = ""; }
  }

  app.addEventListener("click", function (event) {
    const backdrop = event.target.closest(".sheet-backdrop");
    if (backdrop && event.target === backdrop) { model.overlay = null; render(false); return; }
    const target = event.target.closest("[data-action]");
    if (!target || !app.contains(target)) return;
    const action = target.dataset.action;
    if (action === "open-source") { model.overlay = target.dataset.source; model.opener = { action: "open-source", source: target.dataset.source }; model.formatOpen = false; render(true); }
    else if (action === "close-sheet" || action === "backdrop") { const opener = model.opener; model.overlay = null; render(false); focusOpener(opener); }
    else if (action === "toggle-format") { model.opener = { action: "toggle-format" }; model.formatOpen = !model.formatOpen; render(true); }
    else if (action === "close-format") { const opener = model.opener; model.formatOpen = false; render(false); focusOpener(opener); }
    else if (action === "choose-format") { window.location.assign(apiUrl("/api/library/export?format=" + encodeURIComponent(target.dataset.format))); }
    else if (action === "manage-sources") { model.opener = { action: "manage-sources" }; model.overlay = model.snapshot.sources.spotify.imported || model.snapshot.sources.spotify.state === "attention" ? "spotify" : "soundcloud"; render(true); }
    else if (action === "import-soundcloud") { importSoundCloud(); }
    else if (action === "update-spotify") { updateSpotify(); }
    else if (action === "save-spotify-scope") { saveSpotifyScope(); }
    else if (action === "reconnect-source") { window.location.assign(apiUrl("/api/auth/spotify/start")); }
    else if (action === "view-imported") { model.overlay = null; render(false); document.getElementById("main-content").focus(); }
    else if (action === "find-charts" || action === "retry-matching") { startMatching(); }
    else if (action === "cohort-placeholder") { announce("Chart choices will be available in the next step."); }
    else if (action === "retry") { refresh(); }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && (model.overlay || model.formatOpen)) {
      event.preventDefault();
      const opener = model.opener;
      model.overlay = null;
      model.formatOpen = false;
      render(false);
      focusOpener(opener);
      return;
    }
    const dialog = app.querySelector('[role="dialog"]');
    if (event.key !== "Tab" || !dialog) return;
    const items = focusables(dialog);
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });

  refresh();
}());
