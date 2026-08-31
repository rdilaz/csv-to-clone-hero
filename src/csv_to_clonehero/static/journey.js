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
    spotifyScope: null,
    selections: null,
    selectionError: "",
    cohortPreference: "",
    confirmationVisible: false,
    pendingRow: "",
    reviewRevision: 0,
    reviewController: null,
    reviewTimer: null,
    downloadView: false,
    preflight: null,
    preflightDestination: "",
    preflightNotice: "",
    review: {
      tracks: [],
      page: null,
      filter: "all",
      search: "",
      loading: false,
      loaded: false,
      error: ""
    }
  };

  const icons = {
    arrow: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h13M13 6l6 6-6 6"/></svg>',
    check: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6"/></svg>',
    close: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg>',
    download: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v12m0 0 5-5m-5 5-5-5M5 20h14"/></svg>',
    folder: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7h7l2 2h9v10H3z"/></svg>',
    search: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6.5"/><path d="m16 16 4 4"/></svg>',
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

  function bytes(value) {
    if (value == null || Number.isNaN(Number(value))) return "Not available";
    const amount = Math.max(0, Number(value));
    if (amount < 1024) return number(amount) + " B";
    const units = ["KB", "MB", "GB", "TB"];
    let size = amount / 1024;
    let unit = units[0];
    for (let index = 1; index < units.length && size >= 1024; index += 1) {
      size /= 1024;
      unit = units[index];
    }
    return size.toLocaleString("en-US", { maximumFractionDigits: size >= 100 ? 0 : 1 }) + " " + unit;
  }

  function destinationName(value) {
    const parts = String(value || "Songs folder").split(/[\\/]+/).filter(Boolean);
    return parts.length ? parts[parts.length - 1] : "Songs folder";
  }

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
    const errorCode = matching.error_code;
    const diagnosticId = matching.diagnostic_id;
    const nextStep = matching.details && matching.details.failure_next_step;
    const failure = errorCode ? '<p class="failure-reference"><strong>Error code:</strong> ' + escapeHtml(errorCode) + (diagnosticId ? '<br><strong>Diagnostic ID:</strong> ' + escapeHtml(diagnosticId) : '') + (nextStep ? '<br>' + escapeHtml(nextStep) : '') + '</p>' : '';
    return '<details class="details"><summary>Details</summary><p>' +
      (reuse ? "Saved chart work was reused without checking every song again. " : "") +
      "Results are shown only when the current check is complete. " + escapeHtml(expectation || "") +
      '</p>' + failure + '</details>';
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
      copy = '<p class="overline">Find charts</p><h1>We couldn’t finish finding charts</h1><p class="lead-copy">Your music is safe.</p><button class="button primary" data-test="retry-matching" data-action="retry-matching"' + (matching.retry_available ? '' : ' disabled aria-disabled="true"') + '>Try Find Charts again ' + icons.arrow + '</button>' + matchingDetails(matching);
    }
    return shell('<section class="matching-stage stage-section" data-test="' + escapeHtml(state) + '"><div class="matching-visual"><div class="matching-glow"></div>' + ring + '<div class="matching-record" aria-hidden="true"></div></div><div class="matching-copy">' + copy + '</div></section>', "Find charts");
  }

  function exactSelectionItems() {
    return model.selections && Array.isArray(model.selections.items)
      ? model.selections.items.filter(function (item) { return String(item.kind || "exact").toLowerCase() === "exact"; })
      : [];
  }

  function exactSelectionState(snapshot) {
    const result = snapshot.matching.results || {};
    const items = exactSelectionItems();
    const strong = items.filter(function (item) {
      return ["RECOMMENDED", "AUTO_ACCEPT"].includes(String(item.eligibility || "").toUpperCase());
    }).length;
    const review = items.filter(function (item) {
      return ["ELIGIBLE_REVIEW", "REVIEW"].includes(String(item.eligibility || "").toUpperCase());
    }).length;
    const bestMatches = Number(result.best_matches || 0);
    const bestAvailable = Number(result.best_available || 0);
    const recommendations = Number(result.recommendations || 0);
    const matchesBest = bestMatches > 0 && items.length === bestMatches && strong === bestMatches && review === 0;
    const matchesAll = recommendations > 0 && items.length === recommendations && strong === bestMatches && review === bestAvailable;
    let cohort = "";
    if (matchesBest && matchesAll && ["best", "all"].includes(model.cohortPreference)) cohort = model.cohortPreference;
    else if (matchesAll && bestAvailable > 0) cohort = "all";
    else if (matchesBest) cohort = "best";
    else if (matchesAll) cohort = "all";
    return { cohort: cohort, count: items.length };
  }

  function cohortCard(kind, count, title, description, action, selected, pending, enabled) {
    const disabled = pending || !enabled;
    const buttonCopy = pending ? "Saving selection..." : selected ? "Selected" : action;
    return '<article class="cohort-card' + (selected ? " is-selected" : "") + '"><div class="cohort-card-head"><div class="cohort-index" aria-hidden="true">' + (selected ? icons.check : "") + '</div><div><p class="cohort-label">' + (kind === "best" ? "Option A" : "Option B") + '</p><h2><strong>' + number(count) + '</strong> ' + title + '</h2></div>' + (selected ? '<span class="selected-badge">Selected</span>' : '') + '</div><p>' + description + '</p><button class="cohort-action" data-action="select-cohort" data-cohort="' + kind + '" aria-pressed="' + (selected ? "true" : "false") + '"' + (disabled ? ' disabled aria-disabled="true"' : '') + '>' + buttonCopy + (selected ? icons.check : icons.arrow) + '</button></article>';
  }

  function selectionMatchesCard(card) {
    return exactSelectionItems().some(function (item) {
      return String(item.track_key || "") === String(card.track_key || "") || (
        item.spotify_track_id && card.spotify_id && String(item.spotify_track_id) === String(card.spotify_id)
      );
    });
  }

  function recommendationCandidate(card) {
    const candidates = Array.isArray(card.candidates) ? card.candidates : [];
    const wanted = String(card.recommendation_candidate_id || card.best_candidate_id || "");
    return candidates.find(function (candidate) { return String(candidate.candidate_id || "") === wanted; })
      || candidates.find(function (candidate) { return Boolean(candidate.best); })
      || candidates[0]
      || null;
  }

  function displayedCandidate(card) {
    const candidates = Array.isArray(card.candidates) ? card.candidates : [];
    return candidates.find(function (candidate) { return Boolean(candidate.selected); })
      || recommendationCandidate(card);
  }

  function sourceLabel(value) {
    return String(value || "Chart").replace(/[_-]+/g, " ").replace(/\b\w/g, function (letter) { return letter.toUpperCase(); });
  }

  function chartRows() {
    return model.review.tracks.map(function (card) {
      const selected = selectionMatchesCard(card);
      const recommended = recommendationCandidate(card);
      const candidate = displayedCandidate(card) || recommended || {};
      const pending = model.pendingRow === String(card.track_key || "");
      const disabled = Boolean(model.pendingRow) || model.pendingAction === "cohort" || model.selections === null;
      const tierState = String(recommended && recommended.review_state || card.review_state || "");
      const tier = tierState === "strong" ? "Best match" : "Best available";
      const chartTitle = candidate.candidate_title || candidate.title || candidate.name || "Recommended chart";
      const charter = String(candidate.charter || "").trim();
      const concerns = candidate.review && Array.isArray(candidate.review.concerns) ? candidate.review.concerns : [];
      const concern = concerns.length ? String(concerns[0]) : "";
      const source = sourceLabel(candidate.source);
      const accessible = "Use " + String(card.title || "this song") + " by " + String(card.artist || "unknown artist");
      return '<div class="chart-row' + (selected ? " is-row-selected" : "") + '" data-track-key="' + escapeHtml(card.track_key) + '" aria-busy="' + (pending ? "true" : "false") + '"><label class="row-check"><input type="checkbox" data-row-selection data-track-key="' + escapeHtml(card.track_key) + '" aria-label="' + escapeHtml(accessible) + '"' + (selected ? " checked" : "") + (disabled ? ' disabled aria-disabled="true"' : '') + '><span class="custom-check" aria-hidden="true">' + icons.check + '</span></label><span class="chart-art" aria-hidden="true">' + escapeHtml(source.charAt(0)) + '</span><div class="chart-song"><strong>' + escapeHtml(card.title) + '</strong><small>' + escapeHtml(card.artist) + '</small></div><div class="chart-tags"><span class="source-tag">' + escapeHtml(source) + '</span><span class="tier-tag ' + (tierState === "strong" ? "tier-best" : "tier-available") + '">' + tier + '</span></div><div class="chart-version"><span>' + escapeHtml(chartTitle) + '</span>' + (charter ? '<small>Chart by ' + escapeHtml(charter) + '</small>' : '') + (concern ? '<em class="concern-label"><span aria-hidden="true">!</span>' + escapeHtml(concern) + '</em>' : '') + (pending ? '<strong class="row-pending" role="status">Saving...</strong>' : '') + '</div></div>';
    }).join("");
  }

  function confirmationList() {
    const filters = [
      ["all", "All"],
      ["strong", "Best matches"],
      ["review", "Best available"],
      ["selected", "Selected"]
    ].map(function (item) {
      return '<button class="filter-button' + (model.review.filter === item[0] ? " is-active" : "") + '" data-action="set-review-filter" data-review-filter="' + item[0] + '" aria-pressed="' + (model.review.filter === item[0] ? "true" : "false") + '"' + (model.review.loading ? " disabled" : "") + '>' + item[1] + '</button>';
    }).join("");
    const rows = chartRows();
    const page = model.review.page || {};
    const shown = model.review.tracks.length;
    const total = Number(page.total || 0);
    let list = rows;
    if (!rows && model.review.loading) list = '<p class="empty-filter">Loading charts...</p>';
    else if (!rows) list = '<p class="empty-filter">No charts match that search or filter.</p>';
    const status = model.review.loading ? "Updating charts..." : "Showing " + number(shown) + " of " + number(total);
    const more = page.has_more ? '<button class="load-more" data-action="load-more-review"' + (model.review.loading ? " disabled" : "") + '>Load more charts</button>' : "";
    return '<section class="confirmation-list" aria-labelledby="confirmation-heading" aria-busy="' + (model.review.loading ? "true" : "false") + '"><div class="list-head"><div><p class="overline">A quick check</p><h2 id="confirmation-heading">A few representative charts</h2></div><span class="list-note">Optional adjustment</span></div><div class="list-toolbar"><label class="search-field">' + icons.search + '<span class="sr-only">Search charts</span><input id="chart-search" type="search" placeholder="Search songs" value="' + escapeHtml(model.review.search) + '" autocomplete="off"></label><div class="filter-group" aria-label="Filter chart choices">' + filters + '</div></div>' + (model.review.error ? '<p class="confirmation-error" role="alert">' + escapeHtml(model.review.error) + '</p>' : '') + '<div class="list-status" role="status">' + status + '</div><div class="chart-list">' + list + '</div><div class="list-footer"><span>' + (total ? number(shown) + " of " + number(total) + " charts" : "") + '</span>' + more + '</div></section>';
  }

  function chartsFound(snapshot) {
    const result = snapshot.matching.results || {};
    const selection = exactSelectionState(snapshot);
    const activeCohort = selection.cohort;
    const selectionKnown = model.selections !== null;
    const pending = model.pendingAction === "cohort";
    const enabled = Boolean(snapshot.matching.authoritative) && !model.pendingRow;
    const mutationPending = Boolean(model.pendingAction) || Boolean(model.pendingRow);
    let status = '<span class="empty-status-mark" aria-hidden="true"></span><span>Nothing selected yet</span>';
    if (!selectionKnown) status = '<span class="empty-status-mark" aria-hidden="true"></span><span>Selection unavailable</span>';
    else if (selection.count > 0) status = '<span class="status-check">' + icons.check + '</span><strong>' + number(selection.count) + ' selected</strong>';
    const details = '<details class="details"><summary>Details</summary><p>' + number(result.no_suitable) + ' no suitable, ' + number(result.not_playable) + ' not playable, and ' + number(result.no_candidate) + ' had no candidate.</p></details>';
    const continuation = selectionKnown && selection.count > 0
      ? '<button class="button primary" data-test="continue-download" data-action="continue-download"' + (mutationPending ? ' disabled aria-disabled="true"' : '') + '>Continue with ' + number(selection.count) + ' charts ' + icons.arrow + '</button>'
      : '<span class="footer-hint">Choose at least one chart to continue</span>';
    return shell('<section class="found-stage stage-section" data-test="charts-found"><header class="found-head"><div><p class="overline">Confirm charts</p><h1>We found charts for <span>' + number(result.recommendations) + '</span> of your songs.</h1></div><div class="selection-status' + (selection.count > 0 ? " has-selection" : "") + '">' + status + '</div></header><div class="cohort-grid">' + cohortCard("best", result.best_matches, "Best matches", "Most reliable. Artist, title, version, and playability all look right.", "Use " + number(result.best_matches) + " best matches", activeCohort === "best", pending, enabled) + cohortCard("all", result.recommendations, "recommendations", "Includes " + number(result.best_available) + " Best available suggestions. Some may be live, remastered, alternate, or otherwise imperfect versions.", "Use all " + number(result.recommendations) + " recommendations", activeCohort === "all", pending, enabled) + '</div>' + (model.selectionError ? '<p class="cohort-error" role="alert">' + escapeHtml(model.selectionError) + '</p>' : '') + (model.confirmationVisible ? confirmationList() : "") + '<footer class="found-footer"><div class="found-gaps"><span><strong>' + number(result.no_suitable) + '</strong> no suitable</span><span><strong>' + number(result.not_playable) + '</strong> not playable</span><span><strong>' + number(result.no_candidate) + '</strong> no candidate</span></div><div class="found-footer-actions">' + details + continuation + '</div></footer></section>', "Confirm charts");
  }

  function preflightCount(preflight, key, fallback) {
    const value = preflight && preflight[key];
    if (value != null) return Number(value || 0);
    return fallback == null ? null : Number(fallback || 0);
  }

  function preflightCountText(value) {
    return value == null ? "Not available" : number(value);
  }

  function preflightMessages(preflight) {
    const messages = [];
    ["errors", "warnings"].forEach(function (key) {
      const values = preflight && Array.isArray(preflight[key]) ? preflight[key] : [];
      values.forEach(function (value) {
        const message = String(value || "").trim();
        if (message && !messages.includes(message)) messages.push(message);
      });
    });
    return messages;
  }

  function downloadFact(label, value, testName) {
    return '<div><dt>' + label + '</dt><dd' + (testName ? ' data-test="' + testName + '"' : '') + '>' + value + '</dd></div>';
  }

  function folderVisual(preflight, ready) {
    const destination = String(preflight && preflight.destination || model.preflightDestination || "Songs folder");
    return '<div class="songs-folder-visual" data-test="destination-visual"><div class="folder-halo"></div><div class="folder-card' + (ready ? " is-ready" : " needs-attention") + '"><span class="folder-icon">' + icons.folder + '</span><span><small>Destination</small><strong>' + escapeHtml(destinationName(destination)) + '</strong></span><span class="folder-check" aria-hidden="true">' + (ready ? icons.check : "!") + '</span></div></div>';
  }

  function downloadReady() {
    const preflight = model.preflight;
    const persistedCount = exactSelectionItems().length;
    if (model.pendingAction === "preflight" && !preflight) {
      return shell('<section class="ready-download-stage stage-section" data-test="download-preflight" aria-busy="true"><div class="download-ready-copy"><p class="overline">Download</p><h1>Checking your library</h1><p class="lead-copy">Validating your selected charts, destination, and available space.</p><div class="preflight-wait" role="status"><span></span>Running real download preflight...</div></div>' + folderVisual(null, false) + '</section>', "Download");
    }

    const ready = Boolean(preflight && preflight.ok);
    const selected = preflightCount(preflight, "selected_chart_count", persistedCount);
    const reusable = preflightCount(preflight, "reusable_count", preflight && preflight.already_present_count);
    const required = preflightCount(preflight, "download_required_count", preflight && preflight.need_download_count);
    const invalid = preflightCount(preflight, "invalid_count", 0);
    const estimated = preflight && preflight.estimated_bytes;
    const available = preflight && preflight.available_bytes;
    const destination = String(preflight && preflight.destination || model.preflightDestination || "Not resolved");
    const space = ready
      ? '<span class="enough-mark">' + icons.check + '</span>' + (available == null ? "Destination validated" : bytes(available) + " available")
      : '<span class="attention-symbol" aria-hidden="true">!</span>Needs attention';
    const estimate = required === 0 && Number(estimated || 0) === 0 ? "No download needed" : bytes(estimated);
    let facts = downloadFact("Selected charts", number(selected), "preflight-selected");
    facts += downloadFact("Already available", preflightCountText(reusable), "preflight-reusable");
    facts += downloadFact("Need download", preflightCountText(required), "preflight-required");
    if (estimated != null) facts += downloadFact("Estimated download", escapeHtml(estimate), "preflight-estimated");
    facts += downloadFact("Destination", '<span class="destination-value" title="' + escapeHtml(destination) + '">' + escapeHtml(destination) + '</span>', "preflight-destination");
    facts += downloadFact("Space check", space, "preflight-space");
    if (invalid > 0) facts += downloadFact("Needs attention", number(invalid), "preflight-invalid");

    const messages = preflightMessages(preflight);
    const issueList = messages.length ? '<div class="preflight-issues" role="alert"><strong>' + (ready ? "Preflight notes" : "Preflight could not finish") + '</strong><ul>' + messages.map(function (message) { return '<li>' + escapeHtml(message) + '</li>'; }).join("") + '</ul></div>' : "";
    const notice = model.preflightNotice ? '<p class="preflight-notice" role="alert">' + escapeHtml(model.preflightNotice) + '</p>' : "";
    const choosing = model.pendingAction === "folder";
    if (ready) {
      return shell('<section class="ready-download-stage stage-section" data-test="download-ready"><div class="download-ready-copy"><p class="overline">Download</p><h1>Ready to add <span>' + number(selected) + '</span> songs</h1><p class="lead-copy">Your chart choices and destination passed every current check.</p><dl class="download-facts">' + facts + '</dl>' + notice + issueList + '<div class="download-ready-status"><span class="enough-mark">' + icons.check + '</span><span><strong>Ready for download</strong><small>No chart files have been downloaded yet.</small></span></div><div class="download-actions"><button class="button secondary" data-action="change-folder"' + (choosing ? ' disabled aria-disabled="true"' : '') + '>' + (choosing ? "Choosing folder..." : "Change folder") + '</button><button class="text-link quiet-back" data-action="back-to-charts"' + (choosing ? ' disabled aria-disabled="true"' : '') + '>Change chart choices ' + icons.arrow + '</button></div></div>' + folderVisual(preflight, true) + '</section>', "Download");
    }
    return shell('<section class="ready-download-stage stage-section" data-test="download-preflight-error"><div class="download-ready-copy"><p class="overline">Download</p><h1>Your library needs one more check</h1><p class="lead-copy">No chart files were downloaded. Your selected charts are still saved.</p><dl class="download-facts">' + facts + '</dl>' + notice + issueList + '<div class="download-actions"><button class="button secondary" data-action="retry-preflight"' + (choosing ? ' disabled aria-disabled="true"' : '') + '>Retry preflight</button><button class="button quiet" data-action="change-folder"' + (choosing ? ' disabled aria-disabled="true"' : '') + '>' + (choosing ? "Choosing folder..." : "Change folder") + '</button><button class="text-link quiet-back" data-action="back-to-charts"' + (choosing ? ' disabled aria-disabled="true"' : '') + '>Change chart choices ' + icons.arrow + '</button></div></div>' + folderVisual(preflight, false) + '</section>', "Download");
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
    if (snapshot.matching && snapshot.matching.state === "charts_found") content = model.downloadView ? downloadReady() : chartsFound(snapshot);
    else if (snapshot.matching && snapshot.matching.state !== "music_ready") content = matchingVisual(snapshot);
    else if (snapshot.operation && snapshot.operation.state === "importing") content = importing(snapshot);
    else content = snapshot.music.canonical_count ? musicReady(snapshot) : sourcesEmpty(snapshot);
    const stage = snapshot.matching && snapshot.matching.state === "charts_found" ? (model.downloadView ? "Download" : "Confirm charts") : snapshot.matching && snapshot.matching.state !== "music_ready" ? "Find charts" : snapshot.operation.state === "importing" ? "Add music" : snapshot.music.canonical_count ? "Music ready" : "Add music";
    app.innerHTML = '<header class="topbar"><div class="brand"><span class="brand-mark" aria-hidden="true"><i></i><i></i><i></i></span><span class="brand-name">Riff</span><span class="brand-divider" aria-hidden="true"></span><span class="brand-context">music to Clone Hero</span></div><div class="stage-label"><span class="stage-pip" aria-hidden="true"></span><span id="stage-label">' + stage + '</span></div></header>' + content.slice(content.indexOf("<main"));
    app.setAttribute("aria-busy", matchingActive(snapshot) || ["preflight", "folder"].includes(model.pendingAction) ? "true" : "false");
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

  function cancelReviewRequest() {
    if (model.reviewTimer !== null) {
      window.clearTimeout(model.reviewTimer);
      model.reviewTimer = null;
    }
    if (model.reviewController) {
      model.reviewController.abort();
      model.reviewController = null;
    }
    model.reviewRevision += 1;
    model.review.loading = false;
  }

  function reviewState() {
    return {
      all: "eligible",
      strong: "strong",
      review: "review",
      selected: "selected"
    }[model.review.filter] || "eligible";
  }

  function reviewFocus() {
    const active = document.activeElement;
    if (!active) return null;
    if (active.id === "chart-search") {
      return { type: "search", start: active.selectionStart, end: active.selectionEnd };
    }
    if (active.dataset && active.dataset.reviewFilter) {
      return { type: "filter", value: active.dataset.reviewFilter };
    }
    if (active.dataset && active.dataset.action === "load-more-review") return { type: "more" };
    return null;
  }

  function restoreReviewFocus(focus) {
    if (!focus) return;
    let target = null;
    if (focus.type === "search") target = document.getElementById("chart-search");
    else if (focus.type === "filter") {
      target = Array.from(app.querySelectorAll("[data-review-filter]")).find(function (button) {
        return button.dataset.reviewFilter === focus.value;
      });
    } else if (focus.type === "more") target = app.querySelector("[data-action='load-more-review']");
    if (!target) return;
    target.focus();
    if (focus.type === "search" && typeof target.setSelectionRange === "function") {
      const length = target.value.length;
      target.setSelectionRange(Math.min(focus.start == null ? length : focus.start, length), Math.min(focus.end == null ? length : focus.end, length));
    }
  }

  async function loadReview(reset, options) {
    const settings = options || {};
    const snapshot = model.snapshot;
    if (!snapshot || !snapshot.matching || !snapshot.matching.authoritative || !model.confirmationVisible) return false;
    if (model.reviewTimer !== null) {
      window.clearTimeout(model.reviewTimer);
      model.reviewTimer = null;
    }
    if (model.reviewController) model.reviewController.abort();
    const controller = new AbortController();
    model.reviewController = controller;
    const revision = ++model.reviewRevision;
    const focus = reviewFocus();
    const offset = reset ? 0 : model.review.tracks.length;
    if (reset) {
      model.review.tracks = [];
      model.review.page = null;
      model.review.loaded = false;
    }
    model.review.loading = true;
    model.review.error = "";
    if (settings.renderStart !== false) {
      render(false);
      restoreReviewFocus(focus);
    }
    let loaded = false;
    try {
      const path = "/api/review?workspace_id=" + encodeURIComponent(snapshot.workspace_id)
        + "&state=" + encodeURIComponent(reviewState())
        + "&limit=25&offset=" + offset
        + "&sort=artist&search=" + encodeURIComponent(model.review.search.trim());
      const payload = await api(path, { signal: controller.signal });
      if (revision !== model.reviewRevision) return false;
      const incoming = Array.isArray(payload.tracks) ? payload.tracks : [];
      if (reset) model.review.tracks = incoming;
      else {
        const existing = new Set(model.review.tracks.map(function (card) { return String(card.track_key || ""); }));
        model.review.tracks = model.review.tracks.concat(incoming.filter(function (card) {
          return !existing.has(String(card.track_key || ""));
        }));
      }
      model.review.page = payload.page || null;
      model.review.loaded = true;
      loaded = true;
    } catch (error) {
      if (error.name === "AbortError" || revision !== model.reviewRevision) return false;
      model.review.error = "We could not load these chart choices. Try the search or filter again.";
    } finally {
      if (revision === model.reviewRevision) {
        model.review.loading = false;
        model.reviewController = null;
        if (settings.renderEnd !== false) {
          render(false);
          restoreReviewFocus(focus);
        }
      }
    }
    return loaded;
  }

  function resetReview() {
    cancelReviewRequest();
    model.confirmationVisible = false;
    model.pendingRow = "";
    model.review.tracks = [];
    model.review.page = null;
    model.review.filter = "all";
    model.review.search = "";
    model.review.loaded = false;
    model.review.error = "";
  }

  async function refresh() {
    const revision = ++model.requestRevision;
    if (model.controller) model.controller.abort();
    cancelReviewRequest();
    model.controller = new AbortController();
    try {
      const response = await fetch(apiUrl("/api/journey"), { credentials: "same-origin", signal: model.controller.signal });
      const snapshot = await response.json();
      if (!response.ok) throw new Error(snapshot.error || "Your music could not be loaded.");
      if (revision !== model.requestRevision) return;
      if (snapshot.matching && snapshot.matching.state === "charts_found" && snapshot.matching.authoritative) {
        model.selections = await api("/api/discovery/selections?workspace_id=" + encodeURIComponent(snapshot.workspace_id), { signal: model.controller.signal });
        model.cohortPreference = "";
        if (revision !== model.requestRevision) return;
      } else {
        model.selections = null;
        model.cohortPreference = "";
      }
      model.selectionError = "";
      const previous = model.snapshot;
      const wasImporting = previous && previous.operation && previous.operation.state === "importing";
      const wasMatching = previous && previous.matching && ["matching_restoring", "matching_running"].includes(previous.matching.state);
      reconcileElapsed(snapshot);
      model.snapshot = snapshot;
      if (snapshot.matching && snapshot.matching.state === "charts_found" && snapshot.matching.authoritative) {
        model.confirmationVisible = model.confirmationVisible || exactSelectionItems().length > 0;
        if (model.confirmationVisible) await loadReview(true, { renderStart: false, renderEnd: false });
        if (revision !== model.requestRevision) return;
      } else {
        resetReview();
      }
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

  async function selectCohort(cohort) {
    if (model.pendingAction || model.pendingRow || !model.snapshot || !model.snapshot.matching.authoritative) return;
    const scope = cohort === "all" ? "eligible" : cohort === "best" ? "strong" : "";
    if (!scope) return;
    const workspace = model.snapshot.workspace_id;
    const endpoint = "/api/discovery/select-all";
    const headers = { "Content-Type": "application/json" };
    model.pendingAction = "cohort";
    model.selectionError = "";
    render(false);
    try {
      model.selections = await api(endpoint, {
        method: "POST", headers: headers,
        body: JSON.stringify({ workspace_id: workspace, kind: "exact", action: "clear" })
      });
      model.cohortPreference = "";
      render(false);
      model.selections = await api(endpoint, {
        method: "POST", headers: headers,
        body: JSON.stringify({ workspace_id: workspace, kind: "exact", scope: scope })
      });
      model.cohortPreference = cohort;
      model.confirmationVisible = true;
      await loadReview(true);
      const persisted = exactSelectionState(model.snapshot);
      if (persisted.cohort === cohort) {
        announce(number(persisted.count) + " charts selected.");
      } else {
        model.selectionError = "Some recommendations could not be selected. Your saved selection is shown. Try again.";
        announce(model.selectionError);
      }
    } catch (error) {
      model.cohortPreference = "";
      try {
        model.selections = await api("/api/discovery/selections?workspace_id=" + encodeURIComponent(workspace));
        if (model.confirmationVisible) await loadReview(true);
        model.selectionError = "We could not update your chart choices. Your saved selection is shown. Try again.";
      } catch (_refreshError) {
        model.selections = null;
        model.selectionError = "We could not update or verify your chart choices. Retry the selection or reload this page.";
      }
      announce(model.selectionError);
    } finally {
      model.pendingAction = "";
      render(false);
    }
  }

  function focusReviewRow(trackKey) {
    const checkbox = Array.from(app.querySelectorAll("[data-row-selection]")).find(function (input) {
      return String(input.dataset.trackKey || "") === String(trackKey || "");
    });
    if (checkbox) checkbox.focus();
  }

  async function updateRowSelection(trackKey, selected) {
    if (!trackKey || model.pendingRow || model.pendingAction || !model.snapshot || !model.snapshot.matching.authoritative) return;
    const card = model.review.tracks.find(function (item) { return String(item.track_key || "") === String(trackKey); });
    const candidate = card ? recommendationCandidate(card) : null;
    if (selected && (!candidate || candidate.candidate_id == null)) {
      model.review.error = "That recommendation is no longer available. Refresh the chart list and try again.";
      render(false);
      return;
    }
    const workspace = model.snapshot.workspace_id;
    const payload = {
      workspace_id: workspace,
      kind: "exact",
      track_id: trackKey,
      action: selected ? "select" : "clear"
    };
    if (selected) payload.candidate_id = candidate.candidate_id;
    model.pendingRow = String(trackKey);
    model.review.error = "";
    render(false);
    try {
      model.selections = await api("/api/discovery/select", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      model.cohortPreference = "";
      model.confirmationVisible = true;
      const loaded = await loadReview(true, { renderStart: false, renderEnd: false });
      if (!loaded && !model.review.error) {
        model.review.error = "Your choice was saved, but the chart list could not be refreshed. Try the filter again.";
      }
      const count = exactSelectionItems().length;
      announce(selected ? "Chart selected. " + number(count) + " selected." : "Chart unselected. " + number(count) + " selected.");
    } catch (error) {
      model.cohortPreference = "";
      try {
        model.selections = await api("/api/discovery/selections?workspace_id=" + encodeURIComponent(workspace));
        await loadReview(true, { renderStart: false, renderEnd: false });
        model.review.error = "We could not update that chart. Your saved choice was restored. Try again.";
      } catch (_refreshError) {
        model.review.error = "We could not update or verify that chart. Reload this page before trying again.";
      }
      announce(model.review.error);
    } finally {
      model.pendingRow = "";
      render(false);
      focusReviewRow(trackKey);
    }
  }

  async function runPreflight(destination) {
    if (model.pendingAction || model.pendingRow || !model.snapshot || !model.snapshot.matching.authoritative) return;
    const selected = exactSelectionItems().length;
    if (selected < 1) {
      announce("Choose at least one chart before continuing.");
      return;
    }
    model.downloadView = true;
    model.preflight = null;
    model.preflightNotice = "";
    model.preflightDestination = destination == null ? "" : String(destination);
    model.pendingAction = "preflight";
    render(false);
    const payload = { kind: "folder", workspace_id: model.snapshot.workspace_id };
    if (model.preflightDestination) payload.path = model.preflightDestination;
    try {
      const preflight = await api("/api/export/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      model.preflight = preflight;
      model.preflightDestination = String(preflight.destination || model.preflightDestination || "");
      if (preflight.ok) announce("Download preflight passed for " + number(preflight.selected_chart_count) + " charts.");
      else announce((preflight.errors && preflight.errors[0]) || "Download preflight needs attention.");
    } catch (error) {
      model.preflight = {
        ok: false,
        selected_chart_count: selected,
        eligible_chart_count: 0,
        reusable_count: null,
        download_required_count: null,
        invalid_count: 0,
        estimated_bytes: null,
        available_bytes: null,
        destination: model.preflightDestination,
        errors: [error.message],
        warnings: []
      };
      announce(error.message);
    } finally {
      model.pendingAction = "";
      render(false);
    }
  }

  async function changeFolder() {
    if (model.pendingAction || model.pendingRow || !model.downloadView) return;
    model.pendingAction = "folder";
    model.preflightNotice = "";
    render(false);
    try {
      const picked = await api("/api/paths/pick", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}"
      });
      if (!picked.selected) {
        announce("Folder unchanged.");
        return;
      }
      const resolved = String(picked.resolved || picked.path || "");
      if (!resolved) throw new Error("The selected folder could not be resolved.");
      model.pendingAction = "";
      await runPreflight(resolved);
      return;
    } catch (error) {
      model.preflightNotice = "We could not change the folder. " + error.message;
      announce(model.preflightNotice);
    } finally {
      if (model.pendingAction === "folder") {
        model.pendingAction = "";
        render(false);
      }
    }
  }

  function backToCharts() {
    if (model.pendingAction) return;
    model.downloadView = false;
    model.preflight = null;
    model.preflightNotice = "";
    render(false);
    const main = document.getElementById("main-content");
    if (main) main.focus();
    announce(number(exactSelectionItems().length) + " chart choices are still selected.");
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
    else if (action === "select-cohort") { selectCohort(target.dataset.cohort); }
    else if (action === "set-review-filter") {
      model.review.filter = target.dataset.reviewFilter || "all";
      loadReview(true);
    }
    else if (action === "load-more-review") { loadReview(false); }
    else if (action === "continue-download") { runPreflight(); }
    else if (action === "retry-preflight") { runPreflight(model.preflightDestination || undefined); }
    else if (action === "change-folder") { changeFolder(); }
    else if (action === "back-to-charts") { backToCharts(); }
    else if (action === "retry") { refresh(); }
  });

  app.addEventListener("input", function (event) {
    if (event.target.id !== "chart-search") return;
    model.review.search = event.target.value;
    if (model.reviewTimer !== null) window.clearTimeout(model.reviewTimer);
    if (model.reviewController) model.reviewController.abort();
    model.reviewRevision += 1;
    model.review.loading = false;
    model.reviewTimer = window.setTimeout(function () {
      model.reviewTimer = null;
      loadReview(true);
    }, 300);
  });

  app.addEventListener("change", function (event) {
    const checkbox = event.target.closest("[data-row-selection]");
    if (!checkbox || !app.contains(checkbox)) return;
    updateRowSelection(checkbox.dataset.trackKey, checkbox.checked);
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
