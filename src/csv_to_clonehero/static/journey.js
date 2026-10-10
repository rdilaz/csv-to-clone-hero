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
    includeReview: false,
    confirmationVisible: false,
    pendingRow: "",
    reviewRevision: 0,
    reviewController: null,
    reviewTimer: null,
    downloadView: false,
    preflight: null,
    preflightDestination: "",
    preflightNotice: "",
    exportStatus: null,
    exportError: "",
    exportTechnicalError: "",
    exportLastChecked: null,
    exportReconnectAttempts: 0,
    exportPollTimer: null,
    exportController: null,
    exportRevision: 0,
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
    const playlistDetail = source.playlist_count ? ", " + number(source.playlist_count) + " " + (source.playlist_count === 1 ? "playlist" : "playlists") : "";
    const detail = source.song_count || source.playlist_count ? number(source.song_count) + " unique " + (source.song_count === 1 ? "song" : "songs") + playlistDetail : info.description;
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
    const scope = source.playlist_count ? "songs, " + number(source.playlist_count) + " " + (source.playlist_count === 1 ? "playlist" : "playlists") : "songs";
    return '<div class="source-summary-row"><span class="source-summary-name"><span class="mini-source ' + escapeHtml(info.art) + '" aria-hidden="true">' + escapeHtml(info.mark) + '</span>' + escapeHtml(source.label) + '</span><strong>' + number(count) + '</strong><span>' + escapeHtml(scope) + '</span></div>';
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

  function reviewConcerns(candidate) {
    const values = candidate && candidate.review && Array.isArray(candidate.review.concerns)
      ? candidate.review.concerns : [];
    return values.reduce(function (concerns, value) {
      const concern = String(value == null ? "" : value).trim();
      if (concern && !concerns.includes(concern)) concerns.push(concern);
      return concerns;
    }, []);
  }

  function concernPopover(concerns, index) {
    if (!concerns.length) return "";
    const id = "chart-concerns-" + index;
    const label = concerns.length === 1 ? "Chart issue" : number(concerns.length) + " chart issues";
    const reasons = concerns.length === 1
      ? '<p>' + escapeHtml(concerns[0]) + '</p>'
      : '<ul>' + concerns.map(function (concern) { return '<li>' + escapeHtml(concern) + '</li>'; }).join("") + '</ul>';
    return '<div class="chart-concern"><button type="button" class="concern-trigger" data-action="toggle-concern" aria-controls="' + id + '" aria-describedby="' + id + '"><span class="concern-mark" aria-hidden="true">!</span><span>' + label + '</span></button><div class="concern-popover" id="' + id + '" role="tooltip"><strong>Why this chart needs attention</strong>' + reasons + '</div></div>';
  }

  function chartRows() {
    return model.review.tracks.map(function (card, index) {
      const selected = selectionMatchesCard(card);
      const recommended = recommendationCandidate(card);
      const candidate = displayedCandidate(card) || recommended || {};
      const pending = model.pendingRow === String(card.track_key || "");
      const disabled = Boolean(model.pendingRow) || model.pendingAction === "cohort" || model.selections === null;
      const tierState = String(recommended && recommended.review_state || card.review_state || "");
      const tier = tierState === "strong" ? "Best match" : "Best available";
      const chartTitle = candidate.candidate_title || candidate.title || candidate.name || "Recommended chart";
      const charter = String(candidate.charter || "").trim();
      const concerns = reviewConcerns(candidate);
      const coverage = candidate.difficulty_coverage || {};
      const levels = Array.isArray(coverage.guitar_difficulties) ? coverage.guitar_difficulties : [];
      const difficulty = coverage.evidence === "inspected_notes" && levels.length
        ? "Guitar: " + levels.map(function (level) { return level.charAt(0).toUpperCase() + level.slice(1); }).join(" / ") + " (verified notes)"
        : "Difficulty levels unknown";
      const alternatives = Array.isArray(card.candidates) ? card.candidates.filter(function (version) { return String(version.candidate_id) !== String(candidate.candidate_id); }) : [];
      const versions = alternatives.length ? '<details class="chart-alternatives"><summary>Other chart versions (' + number(alternatives.length) + ')</summary>' + alternatives.map(function (version) {
        const eligible = ["strong", "needs_review"].includes(version.review_state);
        return '<div><strong>' + escapeHtml(version.candidate_title || version.title || "Chart version") + '</strong><p>' + escapeHtml(reviewConcerns(version).join(" ") || "Recording metadata supplied by the chart source.") + '</p><button class="button secondary" data-action="select-version" data-track-key="' + escapeHtml(card.track_key) + '" data-candidate-id="' + escapeHtml(version.candidate_id) + '"' + (!eligible || disabled ? ' disabled' : '') + '>Use this version</button></div>';
      }).join("") + '</details>' : '';
      const source = sourceLabel(candidate.source);
      const accessible = "Use " + String(card.title || "this song") + " by " + String(card.artist || "unknown artist");
      return '<div class="chart-row' + (selected ? " is-row-selected" : "") + '" data-track-key="' + escapeHtml(card.track_key) + '" aria-busy="' + (pending ? "true" : "false") + '"><label class="row-check"><input type="checkbox" data-row-selection data-track-key="' + escapeHtml(card.track_key) + '" aria-label="' + escapeHtml(accessible) + '"' + (selected ? " checked" : "") + (disabled ? ' disabled aria-disabled="true"' : '') + '><span class="custom-check" aria-hidden="true">' + icons.check + '</span></label><span class="chart-art" aria-hidden="true">' + escapeHtml(source.charAt(0)) + '</span><div class="chart-song"><strong>' + escapeHtml(card.title) + '</strong><small>' + escapeHtml(card.artist) + '</small></div><div class="chart-tags"><span class="source-tag">' + escapeHtml(source) + '</span><span class="tier-tag ' + (tierState === "strong" ? "tier-best" : "tier-available") + '">' + tier + '</span></div><div class="chart-version"><span>' + escapeHtml(chartTitle) + '</span>' + (charter ? '<small>Chart by ' + escapeHtml(charter) + '</small>' : '') + '<small class="chart-difficulty">' + escapeHtml(difficulty) + '</small>' + concernPopover(concerns, index) + versions + (pending ? '<strong class="row-pending" role="status">Saving...</strong>' : '') + '</div></div>';
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
    return '<section class="confirmation-list" aria-labelledby="confirmation-heading" aria-busy="' + (model.review.loading ? "true" : "false") + '"><div class="list-head"><div><p class="overline">A quick check</p><h2 id="confirmation-heading">Review songs and chart versions</h2></div><span class="list-note">Optional adjustment</span></div><div class="list-toolbar"><label class="search-field">' + icons.search + '<span class="sr-only">Search charts</span><input id="chart-search" type="search" placeholder="Search songs" value="' + escapeHtml(model.review.search) + '" autocomplete="off"></label><div class="filter-group" aria-label="Filter chart choices">' + filters + '</div></div>' + (model.review.error ? '<p class="confirmation-error" role="alert">' + escapeHtml(model.review.error) + '</p>' : '') + '<div class="list-status" role="status">' + status + '</div><div class="chart-list">' + list + '</div><div class="list-footer"><span>' + (total ? number(shown) + " of " + number(total) + " charts" : "") + '</span>' + more + '</div></section>';
  }

  function chartsFound(snapshot) {
    const result = snapshot.matching.results || {};
    const selection = exactSelectionState(snapshot);
    const activeCohort = selection.cohort;
    const selectionKnown = model.selections !== null;
    const pending = model.pendingAction === "cohort";
    const enabled = Boolean(snapshot.matching.authoritative) && !model.pendingRow;
    const mutationPending = Boolean(model.pendingAction) || Boolean(model.pendingRow);
    const undo = model.selections && model.selections.undo;
    const selectionSettings = '<div class="selection-settings"><strong>One preferred chart per song</strong><p>Each option replaces ' + (selectionKnown ? 'your ' + number(selection.count) + ' saved imported-song choices' : 'imported-song choices') + '. Artist discoveries remain separate. Real playable notes are verified during export.</p><p>Undo keeps the last selection change for 30 minutes while these chart results remain current.</p><label><input id="include-review-choices" type="checkbox"' + (model.includeReview ? ' checked' : '') + (mutationPending ? ' disabled' : '') + '> Include uncertain recommendations in the all-recommendations option</label></div>';
    const restoration = '<div class="selection-restoration">' + (undo && undo.available ? '<button class="button secondary" data-action="undo-selection"' + (mutationPending ? ' disabled' : '') + '>Undo last selection change</button>' : '') + (selectionKnown && selection.count ? '<button class="text-link" data-action="clear-selection"' + (mutationPending ? ' disabled' : '') + '>Clear imported-song choices</button>' : '') + '</div>';
    let status = '<span class="empty-status-mark" aria-hidden="true"></span><span>Nothing selected yet</span>';
    if (!selectionKnown) status = '<span class="empty-status-mark" aria-hidden="true"></span><span>Selection unavailable</span>';
    else if (selection.count > 0) status = '<span class="status-check">' + icons.check + '</span><strong>' + number(selection.count) + ' selected</strong>';
    const details = '<details class="details"><summary>Details</summary><p>' + number(result.no_suitable) + ' no suitable, ' + number(result.not_playable) + ' not playable, and ' + number(result.no_candidate) + ' had no candidate.</p></details>';
    const continuation = selectionKnown && selection.count > 0
      ? '<button class="button primary" data-test="continue-download" data-action="continue-download"' + (mutationPending ? ' disabled aria-disabled="true"' : '') + '>Review export for ' + number(selection.count) + ' charts ' + icons.arrow + '</button>'
      : '<span class="footer-hint">Choose at least one chart to continue</span>';
    return shell('<section class="found-stage stage-section" data-test="charts-found"><header class="found-head"><div><p class="overline">Review chart choices</p><h1>We found charts for <span>' + number(result.recommendations) + '</span> of your songs.</h1></div><div class="selection-status' + (selection.count > 0 ? " has-selection" : "") + '">' + status + '</div></header>' + selectionSettings + '<div class="cohort-grid">' + cohortCard("best", result.best_matches, "Best matches", "Artist, title and recording metadata are the strongest matches. File validation happens during export.", "Use " + number(result.best_matches) + " best matches", activeCohort === "best", pending, enabled) + cohortCard("all", result.recommendations, "recommendations", "Includes " + number(result.best_available) + " uncertain suggestions. Check recording differences before including these charts.", "Use all " + number(result.recommendations) + " recommendations", activeCohort === "all", pending, enabled && model.includeReview) + '</div>' + restoration + (model.selectionError ? '<p class="cohort-error" role="alert">' + escapeHtml(model.selectionError) + '</p>' : '') + (model.confirmationVisible ? confirmationList() : "") + '<footer class="found-footer"><div class="found-gaps"><span><strong>' + number(result.no_suitable) + '</strong> no suitable</span><span><strong>' + number(result.not_playable) + '</strong> not playable</span><span><strong>' + number(result.no_candidate) + '</strong> no candidate</span></div><div class="found-footer-actions">' + details + continuation + '</div></footer></section>', "Review chart choices");
  }

  function hostDestinationControls() {
    const capabilities = model.snapshot && model.snapshot.capabilities || {};
    const host = capabilities.host_name || "the Riff host";
    const destination = model.preflightDestination || capabilities.configured_destination || "";
    return '<aside class="host-capability"><strong>Local export to ' + escapeHtml(host) + '</strong><p>Charts are written to a folder on this host. Clone Hero or YARG must scan that folder afterward.</p>' + (!capabilities.native_folder_picker ? '<p>' + escapeHtml(capabilities.folder_picker_reason || "Folder picker capability is unavailable.") + '</p>' : '<p>The native picker opens on this Windows host.</p>') + (capabilities.manual_destination ? '<label for="host-folder-path">Songs folder on this host</label><div class="host-path-controls"><input id="host-folder-path" type="text" value="' + escapeHtml(destination) + '" autocomplete="off"><button class="button secondary" data-action="validate-host-folder"' + (model.pendingAction ? ' disabled' : '') + '>Check this folder</button></div>' : '<p>Use the linked host folder, or open Riff on the host to change the destination.</p>') + '</aside>';
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

  function folderVisual(preflight, state) {
    const destination = String(preflight && preflight.destination || model.preflightDestination || "Songs folder");
    const ready = state === "ready";
    const warning = state === "warning";
    const indicator = ready
      ? '<span class="folder-check" aria-hidden="true">' + icons.check + '</span>'
      : warning ? '<span class="folder-check" aria-hidden="true">!</span>' : "";
    return '<div class="songs-folder-visual" data-test="destination-visual"><div class="folder-halo"></div><div class="folder-card' + (ready ? " is-ready" : warning ? " needs-attention" : "") + '"><span class="folder-icon">' + icons.folder + '</span><span><small>Destination</small><strong>' + escapeHtml(destinationName(destination)) + '</strong></span>' + indicator + '</div></div>';
  }

  function downloadReady() {
    const preflight = model.preflight;
    const persistedCount = exactSelectionItems().length;
    if (model.pendingAction === "preflight" && !preflight) {
      return shell('<section class="ready-download-stage stage-section" data-test="download-preflight" aria-busy="true"><div class="download-ready-copy"><p class="overline">Download</p><h1>Checking your library</h1><p class="lead-copy">Validating your selected charts, destination, and available space.</p><div class="preflight-wait" role="status"><span></span>Running real download preflight...</div></div>' + folderVisual(null, "neutral") + '</section>', "Download");
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
    const starting = model.pendingAction === "export-start";
    const busy = Boolean(model.pendingAction);
    const pickerUnavailable = !(model.snapshot.capabilities && model.snapshot.capabilities.native_folder_picker);
    const start = persistedCount > 0
      ? '<button class="button primary" data-action="start-export"' + (busy ? ' disabled aria-disabled="true"' : '') + '>' + (starting ? "Starting export..." : "Write charts to folder " + icons.download) + '</button>'
      : "";
    if (ready) {
      return shell('<section class="ready-download-stage stage-section" data-test="download-ready"><div class="download-ready-copy"><p class="overline">Download</p><h1>Review export of <span>' + number(selected) + '</span> ' + (selected === 1 ? "chart entry" : "chart entries") + '</h1><p class="lead-copy">Selection, destination and space were checked. Playable chart files are verified during export.</p><dl class="download-facts">' + facts + '</dl>' + hostDestinationControls() + notice + issueList + '<div class="download-ready-status"><span class="enough-mark">' + icons.check + '</span><span><strong>Ready to write charts</strong><small>No new chart files have been written yet.</small></span></div><div class="download-actions">' + start + '<button class="button secondary" data-action="change-folder"' + (busy || pickerUnavailable ? ' disabled aria-disabled="true"' : '') + '>' + (choosing ? "Choosing folder..." : "Change folder") + '</button><button class="text-link quiet-back" data-action="back-to-charts"' + (busy ? ' disabled aria-disabled="true"' : '') + '>Change chart choices ' + icons.arrow + '</button></div></div>' + folderVisual(preflight, "ready") + '</section>', "Download");
    }
    return shell('<section class="ready-download-stage stage-section" data-test="download-preflight-error"><div class="download-ready-copy"><p class="overline">Download</p><h1>Your library needs one more check</h1><p class="lead-copy">No chart files were downloaded. Your selected charts are still saved.</p><dl class="download-facts">' + facts + '</dl>' + hostDestinationControls() + notice + issueList + '<div class="download-actions"><button class="button secondary" data-action="retry-preflight"' + (choosing ? ' disabled aria-disabled="true"' : '') + '>Retry preflight</button><button class="button quiet" data-action="change-folder"' + (choosing || pickerUnavailable ? ' disabled aria-disabled="true"' : '') + '>' + (choosing ? "Choosing folder..." : "Change folder") + '</button><button class="text-link quiet-back" data-action="back-to-charts"' + (choosing ? ' disabled aria-disabled="true"' : '') + '>Change chart choices ' + icons.arrow + '</button></div></div>' + folderVisual(preflight, "warning") + '</section>', "Download");
  }

  function knownNumber() {
    for (let index = 0; index < arguments.length; index += 1) {
      const value = arguments[index];
      if (value != null && value !== "" && Number.isFinite(Number(value))) return Number(value);
    }
    return null;
  }

  function exportState(status) {
    if (!status) return "idle";
    const operation = status.operation || {};
    const rawStatus = String(status.status || "idle").toLowerCase();
    let state = rawStatus === "cancelling" ? "cancelling" : String(operation.state || rawStatus).toLowerCase();
    if (status.running && state !== "cancelling") state = "building";
    if (["success", "completed"].includes(state)) state = "complete";
    return state;
  }

  function exportCounts(status) {
    const payload = status || {};
    const itemSummary = payload.items || {};
    const itemRows = Array.isArray(itemSummary.items) ? itemSummary.items : [];
    const result = payload.result || {};
    const preflightSelected = model.preflight && model.preflight.selected_chart_count;
    const persistedSelected = exactSelectionItems().length;
    const expectedSelected = knownNumber(
      Number(preflightSelected) > 0 ? preflightSelected : null,
      persistedSelected > 0 ? persistedSelected : null,
      Number(result.selected) > 0 ? result.selected : null
    );
    const itemSelected = knownNumber(itemSummary.selected);
    const selected = knownNumber(
      result.selected,
      expectedSelected,
      itemSelected,
      Number(payload.total) > 0 ? payload.total : null
    );
    const itemReady = knownNumber(itemSummary.completed, 0);
    const ready = Math.max(knownNumber(result.ready, 0), itemReady);
    const running = ["building", "cancelling"].includes(exportState(payload));
    const itemAdded = itemRows.filter(function (item) {
      return item.status === "published" && item.result !== "already_present";
    }).length;
    const itemAvailable = itemRows.filter(function (item) {
      return item.status === "duplicate" || item.result === "already_present";
    }).length;
    const added = running ? itemAdded : knownNumber(result.added, itemAdded, 0);
    const alreadyAvailable = running ? itemAvailable : knownNumber(result.already_present, itemAvailable, 0);
    const failed = knownNumber(itemSummary.failed, 0);
    const invalid = knownNumber(itemSummary.invalid, 0);
    const cancelled = running
      ? knownNumber(itemSummary.cancelled, 0)
      : knownNumber(result.cancelled_items, itemSummary.cancelled, 0);
    let remaining = running
      ? (itemSelected != null && itemSelected > 0 ? knownNumber(itemSummary.remaining, 0) : null)
      : knownNumber(result.remaining, itemSummary.remaining);
    if (remaining == null && !running && selected != null) remaining = Math.max(0, selected - ready);
    const progressTotal = running && expectedSelected != null && expectedSelected > 0 && itemSelected === expectedSelected
      ? expectedSelected : null;
    return {
      selected: selected,
      ready: ready,
      progressReady: progressTotal == null ? null : Math.max(0, Math.min(progressTotal, itemReady)),
      progressTotal: progressTotal,
      added: added,
      alreadyAvailable: alreadyAvailable,
      failed: failed,
      invalid: invalid,
      cancelled: cancelled,
      remaining: remaining,
      downloading: knownNumber(itemSummary.downloading, 0),
      retrying: knownNumber(itemSummary.retrying, 0)
    };
  }

  function exportActivity(value, cancelling) {
    if (cancelling) return "Finishing active charts safely";
    const activity = String(value || "").trim();
    const labels = {
      "Checking download sizes": "Checking selected charts",
      "Writing files": "Adding charts to your Songs folder",
      "Writing ZIP archive": "Preparing your library",
      "Cancellation requested; finishing the active chart safely": "Finishing active charts safely"
    };
    if (labels[activity]) return labels[activity];
    return activity
      .replace(/^Publishing\s+/i, "Adding ")
      .replace(/^Downloading\s+/i, "Downloading ")
      || "Preparing selected charts";
  }

  function exportRunningView(status) {
    const counts = exportCounts(status);
    const state = exportState(status);
    const cancelling = state === "cancelling";
    const determinate = counts.progressTotal != null && counts.progressTotal > 0;
    const percent = determinate
      ? counts.progressReady >= counts.progressTotal
        ? 100
        : Math.max(0, Math.min(99, Math.round((counts.progressReady / counts.progressTotal) * 100)))
      : null;
    const transfers = counts.downloading + counts.retrying;
    const failures = counts.failed + counts.invalid;
    return {
      cancelling: cancelling,
      determinate: determinate,
      ready: determinate ? counts.progressReady : null,
      total: determinate ? counts.progressTotal : null,
      percent: percent,
      activity: exportActivity(status.current, cancelling),
      activityLabel: cancelling ? "Stopping safely" : "Current activity",
      activityDetail: cancelling ? "Completed songs are being kept" : "Each chart is validated before it is added",
      transfers: transfers,
      added: counts.added,
      alreadyAvailable: counts.alreadyAvailable,
      failures: failures,
      remaining: counts.remaining == null ? "Not available" : number(counts.remaining),
      elapsed: status.started_at ? elapsed(Math.max(0, Date.now() / 1000 - Number(status.started_at))) : "Not available",
      reassuranceWarning: failures > 0,
      reassuranceTitle: failures > 0 ? "Some charts couldn’t be added." : cancelling ? "Stopping safely." : "No action needed.",
      reassuranceDetail: failures > 0 ? "Completed charts are safe. Retry unavailable charts after this step finishes." : cancelling ? "Charts already completed will stay in your Songs folder." : "Keep the app running; you can leave this screen.",
      error: model.exportError,
      stopDisabled: cancelling || model.pendingAction === "cancel"
    };
  }

  function exportGoalComplete(status) {
    if (!(status && status.receipt && status.receipt.verified_at_export && status.receipt.kind === "host_folder")) return false;
    const state = exportState(status);
    if (state === "complete") return true;
    if (state !== "partial") return false;
    const payload = status || {};
    const result = payload.result || {};
    const items = payload.items || {};
    const selected = knownNumber(result.selected, items.selected);
    const ready = knownNumber(result.ready, items.completed, 0);
    return selected != null && selected > 0 && ready >= selected;
  }

  function readyProgressText(view) {
    return view.percent + "% ready, " + view.ready + " of " + view.total + " " + (view.total === 1 ? "chart" : "charts") + " ready";
  }

  function downloadRunning() {
    const status = model.exportStatus || {};
    if (model.exportError) {
      const checked = model.exportLastChecked ? new Date(model.exportLastChecked).toLocaleTimeString() : "Unavailable";
      return shell('<section class="export-recovery stage-section" data-test="export-status-unavailable"><p class="overline">Local export</p><h1>Export status unavailable</h1><p class="lead-copy">The outcome is unknown. Work may still continue on the Riff host. Check its status before starting another export.</p><p>Last successful check: ' + escapeHtml(checked) + '. Your chart choices remain saved.</p><button class="button primary" data-action="check-export-status">Check export status</button><details class="details"><summary>Technical details</summary><p>' + escapeHtml(model.exportTechnicalError) + '</p></details></section>', "Export status unavailable");
    }
    const view = exportRunningView(status);
    const countValue = view.determinate ? number(view.ready) : "";
    const countCopy = view.determinate ? " / " + number(view.total) + " ready" : "Preparing selected charts";
    const progressAttributes = view.determinate
      ? ' aria-label="Charts ready" aria-valuemin="0" aria-valuemax="' + view.total + '" aria-valuenow="' + view.ready + '" aria-valuetext="' + readyProgressText(view) + '"'
      : ' aria-label="Preparing chart progress"';
    const warningLabel = view.failures > 0
      ? "Warning: " + number(view.failures) + (view.failures === 1 ? " chart couldn’t" : " charts couldn’t") + " be added"
      : "";
    const stop = '<button class="button quiet stop-safe" data-action="cancel-export"' + (view.stopDisabled ? ' disabled aria-disabled="true"' : '') + '>' + (view.stopDisabled ? "Stopping safely..." : "Stop safely") + '</button>';
    return shell(
      '<section class="building-stage stage-section" data-test="export-' + (view.cancelling ? "cancelling" : "running") + '">' +
      '<header class="building-head"><div><p class="overline">Download</p><h1>Writing charts to this host</h1><p class="building-count" role="status" aria-live="polite" aria-atomic="true"><strong data-export-ready' + (view.determinate ? "" : " hidden") + '>' + countValue + '</strong><span data-export-count-copy>' + countCopy + '</span></p></div>' +
      '<div class="building-percent' + (view.determinate ? "" : " is-preparing") + '" data-export-percent-wrap><strong data-export-percent>' + (view.determinate ? number(view.percent) + "%" : "Preparing") + '</strong><span data-export-percent-label>' + (view.determinate ? "ready" : "charts") + '</span></div></header>' +
      '<div class="building-progress' + (view.determinate ? "" : " is-indeterminate") + '" data-export-progress role="progressbar"' + progressAttributes + '><span data-export-progress-fill' + (view.determinate ? ' style="width:' + view.percent + '%"' : "") + '></span></div>' +
      '<div class="flow-visual" aria-hidden="true"><div class="flow-orbit flow-orbit-one"></div><div class="flow-orbit flow-orbit-two"></div><span class="flow-node flow-node-one"></span><span class="flow-node flow-node-two"></span><span class="flow-node flow-node-three"></span><div class="flow-line"></div><div class="songs-folder-target"><span>' + icons.folder + '</span><strong>Songs</strong><small>library</small></div></div>' +
      '<div class="download-now"><span class="activity-mark">' + icons.download + '<i aria-hidden="true"></i></span><div class="download-now-copy"><small data-export-activity-label>' + view.activityLabel + '</small><h2 data-export-activity>' + escapeHtml(view.activity) + '</h2><p data-export-activity-detail>' + view.activityDetail + '</p></div><span class="in-progress" data-export-transfers' + (view.transfers > 0 ? "" : " hidden") + '><span class="status-symbol" aria-hidden="true"></span><span data-export-transfer-count>' + number(view.transfers) + '</span> active</span></div>' +
      '<div class="download-counters"><span><strong data-export-added>' + number(view.added) + '</strong> added</span><span><strong data-export-available>' + number(view.alreadyAvailable) + '</strong> already available</span><span class="counter-warning" data-export-warning role="status" aria-label="' + escapeHtml(warningLabel) + '"' + (view.failures > 0 ? "" : " hidden") + '><span class="counter-warning-mark" aria-hidden="true">!</span><strong data-export-failures>' + number(view.failures) + '</strong> couldn’t be added</span><span><strong data-export-remaining>' + view.remaining + '</strong> remaining</span></div>' +
      '<div class="building-reassurance"><span class="reassurance-mark' + (view.reassuranceWarning ? " is-warning" : "") + '" data-export-reassurance-mark aria-hidden="true">' + (view.reassuranceWarning ? "!" : icons.check) + '</span><span><strong data-export-reassurance-title>' + view.reassuranceTitle + '</strong><small data-export-reassurance-detail>' + view.reassuranceDetail + '</small></span><p class="elapsed"><span>Elapsed</span><strong data-export-elapsed>' + view.elapsed + '</strong></p></div>' +
      '<p class="build-inline-error" data-export-error role="alert"' + (view.error ? "" : " hidden") + '>' + escapeHtml(view.error) + '</p>' + stop + '</section>',
      "Download"
    );
  }

  function updateText(container, selector, value) {
    const element = container.querySelector(selector);
    if (element && element.textContent !== String(value)) element.textContent = String(value);
    return element;
  }

  function updateDownloadRunning() {
    const status = model.exportStatus || {};
    const view = exportRunningView(status);
    const testName = "export-" + (view.cancelling ? "cancelling" : "running");
    const section = app.querySelector(".building-stage");
    if (!section || section.getAttribute("data-test") !== testName) return false;

    const ready = updateText(section, "[data-export-ready]", view.determinate ? number(view.ready) : "");
    if (ready) ready.hidden = !view.determinate;
    updateText(section, "[data-export-count-copy]", view.determinate ? " / " + number(view.total) + " ready" : "Preparing selected charts");

    const percentWrap = section.querySelector("[data-export-percent-wrap]");
    if (percentWrap) percentWrap.classList.toggle("is-preparing", !view.determinate);
    updateText(section, "[data-export-percent]", view.determinate ? number(view.percent) + "%" : "Preparing");
    updateText(section, "[data-export-percent-label]", view.determinate ? "ready" : "charts");

    const progress = section.querySelector("[data-export-progress]");
    const fill = section.querySelector("[data-export-progress-fill]");
    if (progress) {
      progress.classList.toggle("is-indeterminate", !view.determinate);
      progress.setAttribute("aria-label", view.determinate ? "Charts ready" : "Preparing chart progress");
      ["aria-valuemin", "aria-valuemax", "aria-valuenow", "aria-valuetext"].forEach(function (attribute) {
        if (!view.determinate) progress.removeAttribute(attribute);
      });
      if (view.determinate) {
        progress.setAttribute("aria-valuemin", "0");
        progress.setAttribute("aria-valuemax", String(view.total));
        progress.setAttribute("aria-valuenow", String(view.ready));
        progress.setAttribute("aria-valuetext", readyProgressText(view));
      }
    }
    if (fill) fill.style.width = view.determinate ? view.percent + "%" : "";

    updateText(section, "[data-export-activity-label]", view.activityLabel);
    updateText(section, "[data-export-activity]", view.activity);
    updateText(section, "[data-export-activity-detail]", view.activityDetail);
    const transfers = section.querySelector("[data-export-transfers]");
    if (transfers) transfers.hidden = view.transfers < 1;
    updateText(section, "[data-export-transfer-count]", number(view.transfers));
    updateText(section, "[data-export-added]", number(view.added));
    updateText(section, "[data-export-available]", number(view.alreadyAvailable));
    updateText(section, "[data-export-failures]", number(view.failures));
    updateText(section, "[data-export-remaining]", view.remaining);

    const warning = section.querySelector("[data-export-warning]");
    if (warning) {
      warning.hidden = view.failures < 1;
      warning.setAttribute("aria-label", view.failures > 0 ? "Warning: " + number(view.failures) + (view.failures === 1 ? " chart couldn’t" : " charts couldn’t") + " be added" : "");
    }
    const reassuranceMark = section.querySelector("[data-export-reassurance-mark]");
    if (reassuranceMark) {
      const markerChanged = reassuranceMark.classList.contains("is-warning") !== view.reassuranceWarning;
      reassuranceMark.classList.toggle("is-warning", view.reassuranceWarning);
      if (markerChanged) reassuranceMark.innerHTML = view.reassuranceWarning ? "!" : icons.check;
    }
    updateText(section, "[data-export-reassurance-title]", view.reassuranceTitle);
    updateText(section, "[data-export-reassurance-detail]", view.reassuranceDetail);
    updateText(section, "[data-export-elapsed]", view.elapsed);

    const error = updateText(section, "[data-export-error]", view.error);
    if (error) error.hidden = !view.error;
    const stop = section.querySelector("[data-action='cancel-export']");
    if (stop) {
      stop.disabled = view.stopDisabled;
      stop.textContent = view.stopDisabled ? "Stopping safely..." : "Stop safely";
      if (view.stopDisabled) stop.setAttribute("aria-disabled", "true");
      else stop.removeAttribute("aria-disabled");
    }
    app.setAttribute("aria-busy", "true");
    return true;
  }

  function terminalIssue(status) {
    const result = status && status.result || {};
    const values = [];
    if (status && status.error) values.push(status.error);
    if (Array.isArray(result.errors)) values.push.apply(values, result.errors);
    return String(values.find(function (value) { return String(value || "").trim(); }) || "The library build could not finish.");
  }

  function exportTerminal() {
    const status = model.exportStatus || {};
    const counts = exportCounts(status);
    let state = exportState(status);
    const destination = String(status.destination || status.result && status.result.destination || model.preflightDestination || "");
    const selected = counts.selected == null ? exactSelectionItems().length : counts.selected;
    const notReady = Math.max(0, selected - counts.ready);
    const receipt = status.receipt;
    if (state === "partial" && notReady === 0 && receipt && receipt.verified_at_export) state = "complete";
    if (state === "failed" && counts.ready > 0 && notReady > 0) state = "partial";
    const destinationDetail = destination ? '<p class="done-destination"><span>Destination</span><strong>' + escapeHtml(destination) + '</strong></p>' : "";
    const changeChoices = '<button class="text-link" data-action="back-to-charts">Change chart choices ' + icons.arrow + '</button>';
    let visual = icons.check;
    let heading = '<span>' + number(counts.ready) + '</span> ' + (counts.ready === 1 ? 'verified chart entry available' : 'verified chart entries available');
    let body = '<p class="done-instruction">Export to the Riff host finished. Next: run Scan Songs in Clone Hero or YARG. Riff has not verified the game scan.</p>';
    let actions = changeChoices;
    let className = "is-complete";
    let testName = "export-complete";

    if ((state === "complete" || counts.ready > 0) && !(receipt && receipt.verified_at_export && receipt.kind === "host_folder")) {
      return shell('<section class="export-recovery stage-section" data-test="export-receipt-unavailable"><p class="overline">Local export</p><h1>Export receipt unavailable</h1><p class="lead-copy">The status says complete, but no verified folder receipt is available. Check status before relying on this result.</p><button class="button primary" data-action="check-export-status">Check export status</button></section>', "Check export receipt");
    }

    if (state === "partial") {
      visual = number(notReady);
      heading = '<span>' + number(counts.ready) + '</span> ' + (counts.ready === 1 ? "verified chart entry" : "verified chart entries") + ' available';
      body = '<p class="done-issue"><strong>' + number(notReady) + '</strong> couldn’t be added</p><p class="done-instruction">Completed charts are safe. Retry only the remaining work.</p>';
      actions = '<button class="button secondary" data-action="retry-export">Retry remaining</button>' + changeChoices;
      className = "is-partial";
      testName = "export-partial";
    } else if (["cancelled", "interrupted"].includes(state)) {
      visual = number(counts.ready);
      heading = '<span>' + number(counts.ready) + '</span> ' + (counts.ready === 1 ? "chart is" : "charts are") + ' already ready';
      body = '<p class="done-issue"><strong>' + number(counts.remaining == null ? notReady : counts.remaining) + '</strong> remain</p><p class="done-instruction">Completed charts were kept. Resume only when you are ready.</p>';
      actions = '<button class="button primary" data-action="resume-export">Resume download ' + icons.download + '</button>' + changeChoices;
      className = "is-recovery";
      testName = "export-" + state;
    } else if (state === "failed") {
      visual = "!";
      heading = counts.ready > 0 ? '<span>' + number(counts.ready) + '</span> ' + (counts.ready === 1 ? "verified chart entry" : "verified chart entries") + ' available, but finishing failed' : 'Build failed';
      body = '<p class="done-instruction">' + escapeHtml(terminalIssue(status)) + '</p>' + (counts.ready > 0 ? '<p class="done-issue">Completed chart files remain in your Songs folder.</p>' : '<p class="done-issue"><strong>' + number(selected) + '</strong> ' + (selected === 1 ? "chart" : "charts") + ' selected</p>');
      actions = '<button class="button secondary" data-action="retry-export">Try again</button>' + changeChoices;
      className = "is-failed";
      testName = "export-failed";
    }
    return shell('<section class="done-stage stage-section ' + className + '" data-test="' + testName + '"><div class="done-visual" aria-hidden="true"><div class="done-disc"><span>' + visual + '</span></div><i class="done-orbit done-orbit-one"></i><i class="done-orbit done-orbit-two"></i></div><div class="done-copy"><p class="overline">' + (state === "complete" ? "Done" : "Download") + '</p><h1>' + heading + '</h1>' + body + '<div class="done-summary"><span><strong>' + number(counts.added) + '</strong> added</span><span><strong>' + number(counts.alreadyAvailable) + '</strong> already available</span></div>' + destinationDetail + '<div class="done-actions">' + actions + '</div></div></section>', state === "complete" ? "Done" : "Download");
  }

  function exportScreen() {
    return ["building", "cancelling"].includes(exportState(model.exportStatus))
      ? downloadRunning()
      : exportTerminal();
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
      const itemCount = playlist.item_count === null || playlist.item_count === undefined ? "Count unavailable" : number(playlist.item_count) + " songs";
      return '<label class="scope-option"><input type="checkbox" data-spotify-playlist="' + escapeHtml(playlist.spotify_id) + '"' + checked + (accessible ? "" : " disabled") + '><span><strong>' + escapeHtml(playlist.name || "Untitled playlist") + '</strong><small>' + escapeHtml(accessible ? itemCount : (playlist.access_reason || "Unavailable")) + '</small></span></label>';
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
      const sourceItems = (source.raw_item_count_is_lower_bound ? "at least " : "") + number(source.raw_item_count || source.song_count) + " source items";
      const scopeDetail = (source.playlist_count ? number(source.playlist_count) + " " + (source.playlist_count === 1 ? "playlist" : "playlists") + (source.include_liked ? " + Liked Songs" : "") + "; " : "") + sourceItems + " became " + number(source.song_count) + " unique " + (source.song_count === 1 ? "song" : "songs") + (source.playlist_only_count ? ", including " + number(source.playlist_only_count) + " playlist-only" : "") + ".";
      body = '<p class="sheet-lead">Your imported music is part of this library.</p><div class="sheet-stat"><strong>' + number(source.song_count) + '</strong><span>unique songs imported</span></div><p class="sheet-updated">' + escapeHtml(scopeDetail) + '</p><p class="sheet-updated">Last updated ' + escapeHtml(date) + '</p>' + (source.state === "attention" ? '<div class="attention-note"><span class="attention-symbol">!</span><span><strong>Reconnect to update</strong><small>Your imported songs are still here.</small></span></div>' : "") + (soundcloud ? '<label class="file-picker"><span>Choose a replacement export</span><input id="soundcloud-file" type="file" accept=".csv,.json,text/csv,application/json"></label>' : "") + '<div class="sheet-actions">' + (spotify ? spotifyActions : '<button class="button primary" data-action="import-soundcloud">Replace import</button>') + '<button class="button quiet" data-action="view-imported">View imported music</button></div>' + (spotify && model.spotifyScope ? spotifyScopeChooser(model.spotifyScope) : "");
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
    const lifecycle = model.downloadView && model.exportStatus && exportState(model.exportStatus) !== "idle";
    if (snapshot.matching && snapshot.matching.state === "charts_found") content = lifecycle ? exportScreen() : model.downloadView ? downloadReady() : chartsFound(snapshot);
    else if (snapshot.matching && snapshot.matching.state !== "music_ready") content = matchingVisual(snapshot);
    else if (snapshot.operation && snapshot.operation.state === "importing") content = importing(snapshot);
    else content = snapshot.music.canonical_count ? musicReady(snapshot) : sourcesEmpty(snapshot);
    const terminalDone = lifecycle && exportGoalComplete(model.exportStatus);
    const stage = snapshot.matching && snapshot.matching.state === "charts_found" ? (terminalDone ? "Done" : model.downloadView ? "Download" : "Confirm charts") : snapshot.matching && snapshot.matching.state !== "music_ready" ? "Find charts" : snapshot.operation.state === "importing" ? "Add music" : snapshot.music.canonical_count ? "Music ready" : "Add music";
    app.innerHTML = '<header class="topbar"><div class="brand"><span class="brand-mark" aria-hidden="true"><i></i><i></i><i></i></span><span class="brand-name">Riff</span><span class="brand-divider" aria-hidden="true"></span><span class="brand-context">local export to this host</span></div><div class="stage-label"><span class="stage-pip" aria-hidden="true"></span><span id="stage-label">' + stage + '</span></div></header>' + content.slice(content.indexOf("<main"));
    app.setAttribute("aria-busy", matchingActive(snapshot) || ["preflight", "folder", "export-start", "cancel"].includes(model.pendingAction) || lifecycle && ["building", "cancelling"].includes(exportState(model.exportStatus)) ? "true" : "false");
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

  function setConcernOpen(concern, open) {
    if (!concern) return;
    concern.classList.toggle("is-open", open);
    if (open) concern.classList.remove("is-dismissed");
  }

  function closeConcernPopovers(except) {
    app.querySelectorAll(".chart-concern.is-open").forEach(function (concern) {
      if (concern !== except) setConcernOpen(concern, false);
    });
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

  function cancelExportPolling() {
    if (model.exportPollTimer !== null) {
      window.clearTimeout(model.exportPollTimer);
      model.exportPollTimer = null;
    }
    if (model.exportController) {
      model.exportController.abort();
      model.exportController = null;
    }
    model.exportRevision += 1;
  }

  function scheduleExportPoll(delay) {
    if (model.exportPollTimer !== null) window.clearTimeout(model.exportPollTimer);
    model.exportPollTimer = window.setTimeout(function () {
      model.exportPollTimer = null;
      loadExportStatus({ render: true });
    }, delay || 1500);
  }

  async function loadExportStatus(options) {
    const settings = options || {};
    if (!model.snapshot || !model.snapshot.workspace_id) return null;
    const previousState = exportState(model.exportStatus);
    if (model.exportController) model.exportController.abort();
    const controller = new AbortController();
    model.exportController = controller;
    const revision = ++model.exportRevision;
    try {
      const status = await api(
        "/api/export/status?workspace_id=" + encodeURIComponent(model.snapshot.workspace_id),
        { signal: controller.signal }
      );
      if (revision !== model.exportRevision) return null;
      const recovering = Boolean(model.exportError);
      model.exportReconnectAttempts = 0;
      model.exportLastChecked = Date.now();
      const state = exportState(status);
      if (state !== "idle") {
        model.exportStatus = status;
        model.downloadView = true;
        model.exportError = "";
        if (status.destination) model.preflightDestination = String(status.destination);
      } else if (settings.rehydrate) {
        model.exportStatus = null;
      }
      if (settings.render !== false) {
        const updated = !recovering && state === previousState && ["building", "cancelling"].includes(state)
          ? updateDownloadRunning() : false;
        if (!updated) render(false);
      }
      if (["building", "cancelling"].includes(state)) scheduleExportPoll();
      return status;
    } catch (error) {
      if (error.name === "AbortError" || revision !== model.exportRevision) return null;
      if (model.exportStatus && ["building", "cancelling"].includes(exportState(model.exportStatus))) {
        model.exportError = "Export status could not be refreshed; its outcome is unknown.";
        model.exportTechnicalError = error.message;
        if (settings.render !== false) render(false);
        model.exportReconnectAttempts += 1;
        if (model.exportReconnectAttempts < 3) scheduleExportPoll(1500 * Math.pow(2, model.exportReconnectAttempts));
      }
      return null;
    } finally {
      if (revision === model.exportRevision) model.exportController = null;
    }
  }

  async function refresh() {
    const revision = ++model.requestRevision;
    if (model.controller) model.controller.abort();
    cancelReviewRequest();
    cancelExportPolling();
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
        await loadExportStatus({ render: false, rehydrate: true });
        if (revision !== model.requestRevision) return;
      } else {
        resetReview();
        model.exportStatus = null;
        model.downloadView = false;
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
    const endpoint = "/api/discovery/select-cohort";
    if (scope === "eligible" && !model.includeReview) return;
    const headers = { "Content-Type": "application/json" };
    model.pendingAction = "cohort";
    model.selectionError = "";
    render(false);
    try {
      model.selections = await api(endpoint, {
        method: "POST", headers: headers,
        body: JSON.stringify({ workspace_id: workspace, scope: scope, confirm_review: model.includeReview,
          expected_revision: model.selections && model.selections.selection_revision })
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

  async function updateRowSelection(trackKey, selected, versionId) {
    if (!trackKey || model.pendingRow || model.pendingAction || !model.snapshot || !model.snapshot.matching.authoritative) return;
    const card = model.review.tracks.find(function (item) { return String(item.track_key || "") === String(trackKey); });
    const candidate = card ? versionId ? card.candidates.find(function (version) { return String(version.candidate_id) === String(versionId); }) : recommendationCandidate(card) : null;
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
    payload.expected_revision = model.selections && model.selections.selection_revision;
    if (selected) payload.candidate_id = candidate.candidate_id;
    model.pendingRow = String(trackKey);
    model.review.error = "";
    render(false);
    try {
      model.selections = await api("/api/discovery/selection/change", {
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
        model.review.error = "We could not confirm that change. Your current saved choices are shown. Check them before trying again.";
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

  async function changeSelectionTray(action) {
    if (model.pendingAction || model.pendingRow || !model.selections) return;
    const workspace = model.snapshot.workspace_id;
    const undo = action === "undo-selection";
    const record = model.selections.undo;
    if (undo && !(record && record.available)) return;
    model.pendingAction = "selection";
    render(false);
    try {
      model.selections = await api(undo ? "/api/discovery/selection/undo" : "/api/discovery/selection/change", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workspace_id: workspace, action: "clear",
          expected_revision: model.selections.selection_revision, operation_id: record && record.operation_id })
      });
      model.cohortPreference = "";
      await loadReview(true);
      announce(undo ? "Previous chart choices restored." : "Imported-song choices cleared." + (model.selections.undo && model.selections.undo.available ? " Undo is available for the last change." : ""));
    } catch (error) {
      model.selectionError = "The change could not be confirmed. Refresh saved choices before trying again.";
      try { model.selections = await api("/api/discovery/selections?workspace_id=" + workspace); }
      catch (_) { model.selections = null; }
      announce(model.selectionError);
    } finally { model.pendingAction = ""; render(false); }
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
    return model.preflight;
  }

  async function changeFolder() {
    if (model.pendingAction || model.pendingRow || !model.downloadView) return;
    if (!model.snapshot.capabilities || !model.snapshot.capabilities.native_folder_picker) return;
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

  async function startExport() {
    if (model.pendingAction || model.pendingRow || !model.preflight || !model.preflight.ok) return;
    if (exactSelectionItems().length < 1) {
      model.preflightNotice = "Choose at least one chart before starting the download.";
      render(false);
      return;
    }
    const destination = String(model.preflight.destination || model.preflightDestination || "");
    if (!destination) {
      model.preflightNotice = "The destination is no longer available. Run preflight again.";
      render(false);
      return;
    }
    model.pendingAction = "export-start";
    model.preflightNotice = "";
    model.exportError = "";
    render(false);
    try {
      const started = await api("/api/spotify/export/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          kind: "folder",
          workspace_id: model.snapshot.workspace_id,
          path: destination
        })
      });
      if (!started.started) throw new Error("The library download did not start.");
      model.downloadView = true;
      model.exportStatus = {
        running: true,
        status: "starting",
        workspace_id: started.workspace_id || model.snapshot.workspace_id,
        destination: started.destination || destination,
        done: 0,
        total: 0,
        current: "Preparing selected charts",
        started_at: null,
        items: {}
      };
      const live = await loadExportStatus({ render: false });
      if (!live) scheduleExportPoll();
      announce("Library download started.");
    } catch (error) {
      model.exportStatus = null;
      model.preflightNotice = "We could not start the download. " + error.message;
      announce(model.preflightNotice);
    } finally {
      model.pendingAction = "";
      render(false);
    }
  }

  async function restartExport() {
    if (model.pendingAction || model.pendingRow) return;
    const destination = String(
      model.exportStatus && model.exportStatus.destination
      || model.exportStatus && model.exportStatus.result && model.exportStatus.result.destination
      || model.preflightDestination
      || ""
    );
    cancelExportPolling();
    model.exportStatus = null;
    model.exportError = "";
    const preflight = await runPreflight(destination || undefined);
    if (preflight && preflight.ok) await startExport();
  }

  async function cancelExport() {
    if (model.pendingAction || !model.exportStatus || !["building", "cancelling"].includes(exportState(model.exportStatus))) return;
    model.pendingAction = "cancel";
    model.exportError = "";
    render(false);
    try {
      const response = await api("/api/spotify/export/cancel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}"
      });
      if (response.status === "cancelling") {
        model.exportStatus = Object.assign({}, model.exportStatus, {
          running: true,
          status: "cancelling",
          current: "Finishing active charts safely",
          operation: Object.assign({}, model.exportStatus.operation || {}, { state: "cancelling" })
        });
        announce("Safe stop requested. Completed songs will be kept.");
      }
      await loadExportStatus({ render: false });
    } catch (error) {
      model.exportError = "We could not request a safe stop. " + error.message;
      announce(model.exportError);
    } finally {
      model.pendingAction = "";
      render(false);
      if (model.exportStatus && ["building", "cancelling"].includes(exportState(model.exportStatus))) scheduleExportPoll();
    }
  }

  function backToCharts() {
    if (model.pendingAction) return;
    cancelExportPolling();
    model.downloadView = false;
    model.preflight = null;
    model.preflightNotice = "";
    model.exportStatus = null;
    model.exportError = "";
    render(false);
    const main = document.getElementById("main-content");
    if (main) main.focus();
    announce(number(exactSelectionItems().length) + " chart choices are still selected.");
  }

  app.addEventListener("click", function (event) {
    const backdrop = event.target.closest(".sheet-backdrop");
    if (backdrop && event.target === backdrop) { model.overlay = null; render(false); return; }
    const concernTrigger = event.target.closest("[data-action='toggle-concern']");
    closeConcernPopovers(concernTrigger && concernTrigger.closest(".chart-concern"));
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
    else if (action === "select-version") { updateRowSelection(target.dataset.trackKey, true, target.dataset.candidateId); }
    else if (action === "undo-selection" || action === "clear-selection") { changeSelectionTray(action); }
    else if (action === "validate-host-folder") {
      const input = document.getElementById("host-folder-path");
      if (input && input.value.trim()) runPreflight(input.value.trim());
    }
    else if (action === "set-review-filter") {
      model.review.filter = target.dataset.reviewFilter || "all";
      loadReview(true);
    }
    else if (action === "load-more-review") { loadReview(false); }
    else if (action === "toggle-concern") {
      const concern = target.closest(".chart-concern");
      if (!concern) return;
      if (concern.classList.contains("is-open") && !concern.classList.contains("is-dismissed")) {
        setConcernOpen(concern, false);
        concern.classList.add("is-dismissed");
      } else {
        setConcernOpen(concern, true);
      }
    }
    else if (action === "continue-download") { runPreflight(); }
    else if (action === "retry-preflight") { runPreflight(model.preflightDestination || undefined); }
    else if (action === "check-export-status") { model.exportReconnectAttempts = 0; loadExportStatus({ render: true }); }
    else if (action === "change-folder") { changeFolder(); }
    else if (action === "start-export") { startExport(); }
    else if (action === "cancel-export") { cancelExport(); }
    else if (["resume-export", "retry-export"].includes(action)) { restartExport(); }
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
    if (event.target.id === "include-review-choices") {
      model.includeReview = event.target.checked;
      render(false);
      document.getElementById("include-review-choices")?.focus();
      return;
    }
    const checkbox = event.target.closest("[data-row-selection]");
    if (!checkbox || !app.contains(checkbox)) return;
    updateRowSelection(checkbox.dataset.trackKey, checkbox.checked);
  });

  app.addEventListener("focusout", function (event) {
    const concern = event.target.closest(".chart-concern");
    if (!concern || event.relatedTarget == null || concern.contains(event.relatedTarget)) return;
    setConcernOpen(concern, false);
    concern.classList.remove("is-dismissed");
  });

  app.addEventListener("pointerout", function (event) {
    const concern = event.target.closest(".chart-concern.is-dismissed");
    if (!concern || concern.contains(event.relatedTarget) || concern.contains(document.activeElement)) return;
    concern.classList.remove("is-dismissed");
  });

  document.addEventListener("keydown", function (event) {
    const activeConcern = document.activeElement && document.activeElement.closest
      ? document.activeElement.closest(".chart-concern") : null;
    const openConcern = app.querySelector(".chart-concern.is-open");
    const hoveredConcern = app.querySelector(".chart-concern:hover");
    if (event.key === "Escape" && (activeConcern || openConcern || hoveredConcern)) {
      event.preventDefault();
      closeConcernPopovers();
      if (activeConcern) activeConcern.classList.add("is-dismissed");
      else if (openConcern) openConcern.classList.add("is-dismissed");
      else hoveredConcern.classList.add("is-dismissed");
      return;
    }
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
