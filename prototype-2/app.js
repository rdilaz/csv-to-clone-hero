(function () {
  "use strict";

  const fixtures = window.PROTOTYPE_2_FIXTURES;
  const totals = fixtures.totals;
  const params = new URLSearchParams(window.location.search);
  const requestedState = params.get("state");
  const initialState = fixtures.states.includes(requestedState) ? requestedState : "sources-empty";
  const initialResult = initialState === "done" && params.get("result") === "partial" ? "partial" : "complete";
  const cleanShot = params.get("clean") === "1";

  const model = {
    state: initialState,
    cohort: initialState === "charts-found" && params.get("selection") === "all" ? "all" : initialState === "download-ready" ? "all" : null,
    selectionMade: initialState === "charts-found" && params.get("selection") === "all",
    result: initialResult,
    overlay: null,
    formatOpen: false,
    filter: "all",
    search: "",
    rowSelection: {},
    sourceStatus: {}
  };

  fixtures.sources.forEach(function (source) {
    model.sourceStatus[source.id] = {
      imported: initialState !== "sources-empty" && !source.comingSoon,
      attention: initialState !== "sources-empty" && source.id === "spotify",
      working: false
    };
  });

  if (cleanShot) document.body.classList.add("clean-shot");

  const app = document.getElementById("app");
  const announcer = document.getElementById("announcer");

  const icons = {
    arrow: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h13M13 6l6 6-6 6"/></svg>',
    arrowUp: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5m-6 6 6-6 6 6"/></svg>',
    check: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6"/></svg>',
    close: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg>',
    download: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v12m0 0 5-5m-5 5-5-5M5 20h14"/></svg>',
    folder: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7h7l2 2h9v10H3z"/></svg>',
    info: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 10v6M12 7h.01"/></svg>',
    pause: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14M16 5v14"/></svg>',
    search: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></svg>',
    sparkle: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 3 1.6 5.4L19 10l-5.4 1.6L12 17l-1.6-5.4L5 10l5.4-1.6L12 3ZM19 16l.7 2.3L22 19l-2.3.7L19 22l-.7-2.3L16 19l2.3-.7L19 16Z"/></svg>',
    vinyl: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="2"/><path d="M12 3.5v3M20.5 12h-3M12 20.5v-3M3.5 12h3"/></svg>'
  };

  function number(value) {
    return Number(value).toLocaleString("en-US");
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>'"]/g, function (character) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[character];
    });
  }

  function announce(message) {
    announcer.textContent = message;
  }

  function sourceById(id) {
    return fixtures.sources.find(function (source) { return source.id === id; });
  }

  function cover(art, className) {
    return '<span class="album-art ' + escapeHtml(art) + (className ? " " + className : "") + '" aria-hidden="true"><span></span></span>';
  }

  function sourceMark(source) {
    return '<span class="source-mark ' + escapeHtml(source.art) + '" aria-hidden="true"><span>' + escapeHtml(source.mark) + '</span></span>';
  }

  function button(label, action, className, extra) {
    return '<button class="button ' + (className || "secondary") + '" data-action="' + action + '"' + (extra || "") + '>' + label + '</button>';
  }

  function stageLabel() {
    if (model.state === "sources-empty") return "Add music";
    if (model.state === "music-ready") return "Music ready";
    if (model.state === "matching-running") return "Find charts";
    if (model.state === "charts-found") return "Confirm charts";
    if (model.state === "download-ready" || model.state === "download-running") return "Download";
    return "Done";
  }

  function shell(content) {
    return '<div class="app-shell">' +
      '<header class="topbar"><div class="brand"><span class="brand-mark" aria-hidden="true"><i></i><i></i><i></i></span><span class="brand-name">Riff</span><span class="brand-divider" aria-hidden="true"></span><span class="brand-context">music to Clone Hero</span></div>' +
      '<div class="stage-label"><span class="stage-pip" aria-hidden="true"></span><span>' + stageLabel() + '</span></div></header>' +
      '<main id="main-content" class="main-content" tabindex="-1">' + content + '</main>' +
      (model.formatOpen ? formatChooser() : "") +
      (model.overlay ? sourceSheet(model.overlay) : "") +
      (cleanShot ? "" : fixtureTools()) +
      '</div>';
  }

  function fixtureTools() {
    const stateOptions = fixtures.states.map(function (state) {
      return '<option value="' + state + '"' + (state === model.state ? " selected" : "") + '>' + state + '</option>';
    }).join("");
    let variant = "";
    if (model.state === "charts-found") {
      variant = '<select id="variant-switch" aria-label="Charts found selection"><option value="none"' + (!model.selectionMade ? " selected" : "") + '>nothing selected</option><option value="best"' + (model.selectionMade && model.cohort === "best" ? " selected" : "") + '>best matches</option><option value="all"' + (model.selectionMade && model.cohort === "all" ? " selected" : "") + '>all recommendations</option></select>';
    } else if (model.state === "done") {
      variant = '<select id="variant-switch" aria-label="Done result"><option value="complete"' + (model.result === "complete" ? " selected" : "") + '>complete</option><option value="partial"' + (model.result === "partial" ? " selected" : "") + '>partial</option></select>';
    }
    return '<div class="fixture-tools"><span>Fixture</span><select id="fixture-switch" aria-label="Fixture state">' + stateOptions + '</select>' + variant + '</div>';
  }

  function formatChooser() {
    return '<div class="format-popover" role="dialog" aria-labelledby="format-heading"><div class="format-popover-head"><div><p class="overline">Song list</p><h2 id="format-heading">Choose a format</h2></div><button class="icon-button" data-action="close-format" aria-label="Close format chooser">' + icons.close + '</button></div>' +
      '<div class="format-options"><button class="format-option" data-action="choose-format" data-format="TXT"><span class="format-type">TXT</span><span><strong>Artist — Song</strong><small>Plain text, easy to read</small></span>' + icons.arrow + '</button>' +
      '<button class="format-option" data-action="choose-format" data-format="CSV"><span class="format-type">CSV</span><span><strong>Structured music list</strong><small>One song per row</small></span>' + icons.arrow + '</button>' +
      '<button class="format-option" data-action="choose-format" data-format="JSON"><span class="format-type">JSON</span><span><strong>Structured list with sources</strong><small>Includes source information</small></span>' + icons.arrow + '</button></div></div>';
  }

  function statusCue(status, label) {
    return '<span class="status-cue status-' + status + '"><span class="status-symbol" aria-hidden="true"></span><span>' + label + '</span></span>';
  }

  function sourceTile(source, compact) {
    const state = model.sourceStatus[source.id] || {};
    let status = source.comingSoon ? "coming" : state.imported ? (state.working ? "working" : state.attention ? "attention" : "added") : "not-added";
    let label = source.comingSoon ? "Coming soon" : state.imported ? (state.working ? "Updating" : state.attention ? "Reconnect needed" : "Imported") : "Not added";
    const tag = statusCue(status, label);
    const inner = sourceMark(source) + '<span class="source-tile-copy"><strong>' + escapeHtml(source.label) + '</strong><small>' + escapeHtml(source.description) + '</small>' + tag + '</span>' + (source.comingSoon ? '' : icons.arrow);
    if (source.comingSoon) {
      return '<article class="source-tile source-coming' + (compact ? " compact" : "") + '" aria-label="' + escapeHtml(source.label) + ', coming soon">' + inner + '</article>';
    }
    return '<button class="source-tile source-action' + (compact ? " compact" : "") + '" data-action="open-source" data-source="' + source.id + '" aria-label="Open ' + escapeHtml(source.label) + ' source">' + inner + '</button>';
  }

  function sourcesEmpty() {
    const supported = fixtures.sources.slice(0, 2).map(function (source) { return sourceTile(source); }).join("");
    const future = fixtures.sources.slice(2).map(function (source) { return sourceTile(source); }).join("");
    return shell('<section class="source-stage stage-section"><div class="source-intro"><p class="overline">Add music</p><h1>Start with the music you already love.</h1><p class="lead-copy">Bring your listening history into one library, then find the charts that fit it.</p><div class="journey-note"><span class="journey-note-mark">' + icons.sparkle + '</span><span><strong>One simple path</strong><small>Add music. Choose charts. Play.</small></span></div></div>' +
      '<section class="source-drawer" aria-labelledby="source-drawer-heading"><header class="drawer-head"><div><p class="overline">Your music sources</p><h2 id="source-drawer-heading">Add a source</h2></div><span class="drawer-meta">Ready when you are</span></header><div class="source-grid source-grid-supported">' + supported + '</div><div class="future-heading"><span>On the way</span><span class="future-rule"></span><small>Coming soon</small></div><div class="source-grid source-grid-future">' + future + '</div></section></section>');
  }

  function sourceSummary(source, count) {
    return '<div class="source-summary-row"><span class="source-summary-name"><span class="mini-source ' + escapeHtml(source.art) + '" aria-hidden="true">' + escapeHtml(source.mark) + '</span>' + escapeHtml(source.label) + '</span><strong>' + number(count) + '</strong><span>songs</span></div>';
  }

  function musicReady() {
    const spotify = sourceById("spotify");
    const soundcloud = sourceById("soundcloud");
    return shell('<section class="ready-stage stage-section"><div class="ready-copy"><p class="overline">Music ready</p><h1><span class="hero-number">' + number(totals.uniqueSongs) + '</span><span class="hero-unit">songs</span></h1><p class="lead-copy">' + number(totals.uniqueSongs) + ' unique songs after duplicates were removed.</p><div class="source-summary" aria-label="Music source summary">' + sourceSummary(spotify, totals.spotify) + sourceSummary(soundcloud, totals.soundcloud) + '<div class="source-summary-row shared-row"><span class="source-summary-name"><span class="shared-mark" aria-hidden="true">+</span>In both</span><strong>' + number(totals.shared) + '</strong><span>songs</span></div></div><div class="ready-actions"><div class="button-row">' + button('Find Clone Hero charts ' + icons.arrow, "find-charts", "primary") + '<div class="format-action"><button class="button secondary" data-action="toggle-format" aria-haspopup="dialog" aria-expanded="' + (model.formatOpen ? "true" : "false") + '">' + icons.download + ' Download my song list</button></div></div><button class="text-link" data-action="manage-sources">Manage music sources ' + icons.arrow + '</button></div></div>' +
      '<div class="ready-visual" aria-hidden="true"><div class="source-orbit orbit-spotify"><span>Spotify</span><strong>' + number(totals.spotify) + '</strong></div><div class="source-orbit orbit-soundcloud"><span>SoundCloud</span><strong>' + number(totals.soundcloud) + '</strong></div><div class="orbit-center">' + icons.vinyl + '<span>in one<br>place</span></div><div class="visual-spark spark-one"></div><div class="visual-spark spark-two"></div></div></section>');
  }

  function progressRing(value, max, label) {
    const percent = Math.round((value / max) * 100);
    return '<div class="progress-ring" style="--progress: ' + percent + '%" role="progressbar" aria-label="' + label + '" aria-valuemin="0" aria-valuemax="' + max + '" aria-valuenow="' + value + '"><div class="progress-ring-inner"><strong>' + percent + '%</strong><span>checked</span></div></div>';
  }

  function matchingRunning() {
    const current = fixtures.matching.current;
    return shell('<section class="matching-stage stage-section"><div class="matching-visual"><div class="matching-glow"></div>' + progressRing(fixtures.matching.checked, totals.uniqueSongs, "Songs checked") + '<div class="matching-needle" aria-hidden="true"></div><div class="matching-record" aria-hidden="true"></div></div><div class="matching-copy"><p class="overline">Find charts</p><h1>Finding your Clone Hero charts</h1><p class="matching-count"><strong>' + number(fixtures.matching.checked) + '</strong> / ' + number(totals.uniqueSongs) + '</p><div class="fine-progress" aria-hidden="true"><span style="width:' + Math.round((fixtures.matching.checked / totals.uniqueSongs) * 100) + '%"></span></div><div class="current-check"><div class="current-art">' + cover(current.art) + '<span class="scan-line"></span></div><div><small>Currently checking</small><strong>' + escapeHtml(current.artist) + ' <span>—</span> ' + escapeHtml(current.title) + '</strong></div></div><div class="matching-reassurance"><span class="reassurance-mark">' + icons.check + '</span><span><strong>No action needed.</strong><small>Keep the app running; you can leave this screen.</small></span></div><p class="elapsed"><span>Elapsed</span><strong>' + escapeHtml(fixtures.matching.elapsed) + '</strong></p><details class="details"><summary>Details</summary><p>Charts are checked for song identity, version, instruments, and playability. No chart files are downloaded during this step.</p></details></div></section>');
  }

  function cohortCard(kind, count, title, description, action, selected) {
    return '<article class="cohort-card' + (selected ? " is-selected" : "") + '"><div class="cohort-card-head"><div class="cohort-index" aria-hidden="true">' + (selected ? icons.check : "") + '</div><div><p class="cohort-label">' + (kind === "best" ? "Option A" : "Option B") + '</p><h2><strong>' + number(count) + '</strong> ' + title + '</h2></div>' + (selected ? '<span class="selected-badge">Selected</span>' : '') + '</div><p>' + description + '</p><button class="cohort-action" data-action="select-cohort" data-cohort="' + kind + '">' + (selected ? "Selected" : action) + (selected ? icons.check : icons.arrow) + '</button></article>';
  }

  function chartRows() {
    const search = model.search.trim().toLowerCase();
    return fixtures.confirmationRows.filter(function (row) {
      const matchesSearch = !search || (row.artist + " " + row.title + " " + row.version).toLowerCase().includes(search);
      const matchesFilter = model.filter === "all" || (model.filter === "selected" ? model.rowSelection[row.id] : model.filter === row.tier.toLowerCase().replace(" ", "-"));
      return matchesSearch && matchesFilter;
    });
  }

  function confirmationList() {
    const filterButtons = [
      ["all", "All"],
      ["best-match", "Best matches"],
      ["best-available", "Best available"],
      ["selected", "Selected"]
    ].map(function (item) {
      return '<button class="filter-button' + (model.filter === item[0] ? " is-active" : "") + '" data-action="set-filter" data-filter="' + item[0] + '" aria-pressed="' + (model.filter === item[0] ? "true" : "false") + '">' + item[1] + '</button>';
    }).join("");
    const rows = chartRows().map(function (row) {
      const selected = Boolean(model.rowSelection[row.id]);
      return '<div class="chart-row' + (selected ? " is-row-selected" : "") + '"><label class="row-check"><input type="checkbox" data-row-id="' + row.id + '"' + (selected ? " checked" : "") + '><span class="custom-check" aria-hidden="true">' + icons.check + '</span><span class="sr-only">Use ' + escapeHtml(row.title) + '</span></label>' + cover(row.art, "art-small") + '<div class="chart-song"><strong>' + escapeHtml(row.title) + '</strong><small>' + escapeHtml(row.artist) + '</small></div><div class="chart-tags"><span class="source-tag">' + escapeHtml(row.source) + '</span><span class="tier-tag ' + (row.tier === "Best match" ? "tier-best" : "tier-available") + '">' + escapeHtml(row.tier) + '</span></div><div class="chart-version"><span>' + escapeHtml(row.version) + (row.concern ? '<em class="concern-label"><span aria-hidden="true">!</span>' + escapeHtml(row.concern) + '</em>' : '') + '</span><button class="change-version" data-action="change-version" data-song="' + escapeHtml(row.title) + '">Change version</button></div></div>';
    }).join("");
    return '<section class="confirmation-list" aria-labelledby="confirmation-heading"><div class="list-head"><div><p class="overline">A quick check</p><h2 id="confirmation-heading">A few representative charts</h2></div><span class="list-note">Optional adjustment</span></div><div class="list-toolbar"><label class="search-field">' + icons.search + '<span class="sr-only">Search charts</span><input id="chart-search" type="search" placeholder="Search songs" value="' + escapeHtml(model.search) + '"></label><div class="filter-group" aria-label="Filter representative charts">' + filterButtons + '</div></div><div class="chart-list">' + (rows || '<p class="empty-filter">No representative charts match that filter.</p>') + '</div></section>';
  }

  function chartsFound() {
    const activeCohort = model.selectionMade ? model.cohort : "";
    const selectedCount = activeCohort === "all" ? totals.totalRecommendations : totals.bestMatches;
    const allSelected = activeCohort === "all";
    return shell('<section class="found-stage stage-section"><header class="found-head"><div><p class="overline">Confirm charts</p><h1>We found charts for <span>' + number(totals.totalRecommendations) + '</span> of your songs.</h1></div><div class="selection-status' + (activeCohort ? " has-selection" : "") + '">' + (activeCohort ? '<span class="status-check">' + icons.check + '</span><strong>' + number(selectedCount) + ' selected</strong>' : '<span class="empty-status-mark" aria-hidden="true"></span><span>Nothing selected yet</span>') + '</div></header><div class="cohort-grid">' + cohortCard("best", totals.bestMatches, "Best matches", "Most reliable. Artist, title, version, and playability all look right.", "Use " + number(totals.bestMatches) + " best matches", activeCohort === "best") + cohortCard("all", totals.totalRecommendations, "recommendations", "Includes " + number(totals.bestAvailable) + " Best available suggestions. Some may be live, remastered, alternate, or otherwise imperfect versions.", "Use all " + number(totals.totalRecommendations) + " recommendations", activeCohort === "all") + '</div>' + (activeCohort ? confirmationList() : '') + '<footer class="found-footer"><div class="found-gaps"><span><strong>' + number(totals.noSuitable) + '</strong> no suitable</span><span><strong>' + number(totals.notPlayable) + '</strong> not playable</span><span><strong>' + number(totals.noCandidate) + '</strong> no candidate</span></div>' + (activeCohort ? button('Continue with ' + number(selectedCount) + ' charts ' + icons.arrow, "continue-download", "primary") : '<span class="footer-hint">Choose a cohort to continue</span>') + '</footer></section>');
  }

  function resetRows() {
    model.rowSelection = {};
    fixtures.confirmationRows.forEach(function (row) {
      model.rowSelection[row.id] = Boolean(row.selected);
    });
  }

  if (model.selectionMade) resetRows();

  function selectedDownloadCount() {
    return model.cohort === "all" ? totals.totalRecommendations : totals.bestMatches;
  }

  function downloadReady() {
    const selectedCount = selectedDownloadCount();
    const available = fixtures.downloadReady.alreadyAvailable;
    const needDownload = Math.max(0, selectedCount - available);
    return shell('<section class="ready-download-stage stage-section"><div class="download-ready-copy"><p class="overline">Download</p><h1>Ready to add <span>' + number(selectedCount) + '</span> songs</h1><p class="lead-copy">Your chart choices are set. The rest is one quiet step.</p><dl class="download-facts"><div><dt>Already available</dt><dd>' + number(available) + '</dd></div><div><dt>Need download</dt><dd>' + number(needDownload) + '</dd></div><div><dt>Destination</dt><dd>' + escapeHtml(fixtures.downloadReady.destination) + '</dd></div><div><dt>Space check</dt><dd><span class="enough-mark">' + icons.check + '</span>' + escapeHtml(fixtures.downloadReady.spaceCheck) + '</dd></div></dl>' + button('Download library ' + icons.download, "start-download", "primary") + '<button class="text-link quiet-back" data-action="back-to-charts">Change chart choices ' + icons.arrow + '</button></div><div class="songs-folder-visual"><div class="folder-halo"></div><div class="folder-card"><span class="folder-icon">' + icons.folder + '</span><span><small>Destination</small><strong>Clone Hero<br>/ Songs</strong></span><span class="folder-check">' + icons.check + '</span></div><div class="folder-art-stack" aria-hidden="true">' + cover("cover-4") + cover("cover-2") + cover("cover-5") + '</div></div></section>');
  }

  function downloadRunning() {
    const download = fixtures.downloadRunning;
    const percent = Math.round((download.ready / download.total) * 100);
    return shell('<section class="building-stage stage-section"><header class="building-head"><div><p class="overline">Download</p><h1>Building your Clone Hero library</h1><p class="building-count"><strong>' + number(download.ready) + '</strong> / ' + number(download.total) + ' ready</p></div><div class="building-percent"><strong>' + percent + '%</strong><span>ready</span></div></header><div class="building-progress"><span style="width:' + percent + '%"></span></div><div class="flow-visual" aria-hidden="true"><div class="flow-orbit flow-orbit-one"></div><div class="flow-orbit flow-orbit-two"></div><div class="flow-album flow-album-one">' + cover("cover-1") + '</div><div class="flow-album flow-album-two">' + cover("cover-3") + '</div><div class="flow-album flow-album-three">' + cover("cover-6") + '</div><div class="flow-line"></div><div class="songs-folder-target"><span>' + icons.folder + '</span><strong>Songs</strong><small>library</small></div></div><div class="download-now"><div class="download-art-large">' + cover(download.current.art) + '<span class="download-pulse" aria-hidden="true"></span></div><div class="download-now-copy"><small>Now adding</small><h2>' + escapeHtml(download.current.artist) + ' <span>—</span> ' + escapeHtml(download.current.title) + '</h2><p>Chart downloaded and checked</p></div><span class="in-progress"><span class="status-symbol" aria-hidden="true"></span>' + number(download.inProgress) + ' more in progress</span></div><div class="download-counters"><span><strong>' + number(download.downloaded) + '</strong> downloaded</span><span><strong>' + number(download.alreadyAvailable) + '</strong> already available</span><span class="counter-warning"><strong>' + number(download.couldntAdd) + '</strong> couldn’t be added</span><span><strong>' + number(download.remaining) + '</strong> remaining</span></div><div class="building-reassurance"><span class="reassurance-mark">' + icons.check + '</span><span><strong>No action needed.</strong><small>Keep the app running. Completed songs are kept if the process is interrupted.</small></span><span class="elapsed"><span>Elapsed</span><strong>' + escapeHtml(download.elapsed) + '</strong></span></div><button class="button secondary stop-safe" data-action="stop-safely">' + icons.pause + ' Stop safely</button></section>');
  }

  function done() {
    const partial = model.result === "partial";
    const ready = partial ? totals.totalRecommendations - fixtures.done.partialMissing : totals.totalRecommendations;
    return shell('<section class="done-stage stage-section ' + (partial ? "is-partial" : "is-complete") + '"><div class="done-visual" aria-hidden="true"><div class="done-disc"><span>' + (partial ? number(fixtures.done.partialMissing) : icons.check) + '</span></div><div class="done-art-line">' + cover("cover-2") + cover("cover-5") + cover("cover-1") + '</div></div><div class="done-copy"><p class="overline">Done</p><h1>' + (partial ? '<span>' + number(ready) + '</span> songs are ready' : '<span>' + number(ready) + '</span> songs are ready to play') + '</h1>' + (partial ? '<p class="done-issue"><strong>' + number(fixtures.done.partialMissing) + '</strong> couldn’t be added</p>' : '<p class="done-instruction">In Clone Hero or YARG, scan your Songs folder.</p>') + '<div class="done-actions">' + button('Open Songs Folder ' + icons.folder, "open-folder", "primary") + (partial ? button('Retry ' + number(fixtures.done.partialMissing), "retry-missing", "secondary") : '') + '</div><details class="details"><summary>Details</summary><p>' + (partial ? number(fixtures.done.partialMissing) + ' songs are still available to retry.' : 'Your selected charts are in the Songs folder and ready for Clone Hero or YARG.') + '</p></details></div></section>');
  }

  function sourceSheet(sourceId) {
    const source = sourceById(sourceId);
    if (!source) return "";
    const state = model.sourceStatus[source.id];
    const imported = state.imported;
    const count = totals[source.countKey];
    const status = state.working ? statusCue("working", "Working") : state.attention ? statusCue("attention", "Needs attention") : imported ? statusCue("added", "Imported") : statusCue("not-added", "Not added");
    const actionLabel = source.updateAction;
    const primary = imported ? (state.working ? "Finish update" : actionLabel) : source.action;
    const importedView = imported ? '<div class="sheet-stat"><strong>' + number(count) + '</strong><span>songs added</span></div><p class="sheet-updated">Last updated today</p>' + (state.attention ? '<div class="attention-note"><span class="attention-symbol">!</span><span><strong>Reconnect needed</strong><small>Your imported songs are still here.</small></span></div>' : '') + '<div class="sheet-actions">' + button(primary, "update-source", "primary") + button("Reconnect", "reconnect-source", "secondary") + button("View imported music", "view-imported", "quiet") + '</div><button class="remove-source" data-action="remove-source">Remove this source’s music</button>' : '<div class="sheet-preview"><span class="preview-mark">' + icons.sparkle + '</span><div><strong>Ready to bring it in</strong><small>This fixture has a calm, one-step import.</small></div></div><div class="sheet-actions">' + button(primary, "import-source", "primary") + '</div><p class="sheet-footnote">Fixture only. No sign-in or upload is used here.</p>';
    return '<div class="sheet-backdrop"><aside class="source-sheet" role="dialog" aria-modal="true" aria-labelledby="source-sheet-heading"><header class="sheet-head"><div class="sheet-source-title">' + sourceMark(source) + '<div><p class="overline">Music source</p><h2 id="source-sheet-heading">' + escapeHtml(source.label) + '</h2><div class="sheet-status">' + status + '</div></div></div><button class="icon-button" data-action="close-sheet" aria-label="Close source sheet">' + icons.close + '</button></header><div class="sheet-body">' + (imported ? '<p class="sheet-lead">Your imported music is part of this library.</p>' : '<p class="sheet-lead">Bring this source into your music library.</p>') + importedView + '</div></aside></div>';
  }

  function render() {
    let content;
    if (model.state === "sources-empty") content = sourcesEmpty();
    else if (model.state === "music-ready") content = musicReady();
    else if (model.state === "matching-running") content = matchingRunning();
    else if (model.state === "charts-found") content = chartsFound();
    else if (model.state === "download-ready") content = downloadReady();
    else if (model.state === "download-running") content = downloadRunning();
    else content = done();
    app.innerHTML = content;
    bindControls();
    if (model.overlay) {
      const closeButton = app.querySelector(".source-sheet [data-action=close-sheet]");
      if (closeButton) closeButton.focus();
    }
  }

  function setState(state, message) {
    model.state = state;
    model.overlay = null;
    model.formatOpen = false;
    if (state !== "charts-found") model.selectionMade = false;
    render();
    window.scrollTo(0, 0);
    if (message) announce(message);
  }

  function bindControls() {
    const stateSwitch = document.getElementById("fixture-switch");
    if (stateSwitch) stateSwitch.addEventListener("change", function () {
      model.state = stateSwitch.value;
      model.overlay = null;
      model.formatOpen = false;
      model.selectionMade = false;
      model.cohort = stateSwitch.value === "download-ready" ? "all" : null;
      model.result = "complete";
      model.filter = "all";
      model.search = "";
      render();
    });
    const variantSwitch = document.getElementById("variant-switch");
    if (variantSwitch) variantSwitch.addEventListener("change", function () {
      if (model.state === "charts-found") {
        model.selectionMade = variantSwitch.value !== "none";
        model.cohort = variantSwitch.value === "all" ? "all" : "best";
        if (model.selectionMade) resetRows();
      } else {
        model.result = variantSwitch.value;
      }
      render();
    });
  }

  app.addEventListener("click", function (event) {
    if (event.target.classList.contains("sheet-backdrop")) {
      model.overlay = null;
      render();
      return;
    }
    const target = event.target.closest("[data-action]");
    if (!target || !app.contains(target)) return;
    const action = target.dataset.action;
    if (action === "open-source") {
      model.overlay = target.dataset.source;
      model.formatOpen = false;
      render();
    } else if (action === "close-sheet") {
      model.overlay = null;
      render();
    } else if (action === "import-source") {
      model.sourceStatus[model.overlay].imported = true;
      model.sourceStatus[model.overlay].attention = false;
      model.overlay = null;
      setState("music-ready", "Music source imported.");
    } else if (action === "update-source") {
      const state = model.sourceStatus[model.overlay];
      state.working = !state.working;
      state.attention = false;
      render();
      announce(state.working ? "Source update is working." : "Source update finished.");
    } else if (action === "reconnect-source") {
      model.sourceStatus[model.overlay].attention = false;
      render();
      announce("Source reconnected.");
    } else if (action === "view-imported") {
      model.overlay = null;
      setState("music-ready");
    } else if (action === "remove-source") {
      model.sourceStatus[model.overlay].imported = false;
      model.sourceStatus[model.overlay].attention = false;
      model.sourceStatus[model.overlay].working = false;
      render();
      announce("Source music removed from this fixture.");
    } else if (action === "manage-sources") {
      model.overlay = model.sourceStatus.spotify.imported ? "spotify" : "soundcloud";
      render();
    } else if (action === "toggle-format") {
      model.formatOpen = !model.formatOpen;
      render();
    } else if (action === "close-format") {
      model.formatOpen = false;
      render();
    } else if (action === "choose-format") {
      model.formatOpen = false;
      render();
      announce(target.dataset.format + " song list chosen. No file was downloaded.");
    } else if (action === "find-charts") {
      setState("matching-running", "Chart search started. No action is needed.");
    } else if (action === "select-cohort") {
      model.cohort = target.dataset.cohort;
      model.selectionMade = true;
      resetRows();
      render();
      announce((model.cohort === "all" ? number(totals.totalRecommendations) : number(totals.bestMatches)) + " charts selected.");
    } else if (action === "set-filter") {
      model.filter = target.dataset.filter;
      render();
    } else if (action === "change-version") {
      announce("Version chooser is a fixture-only adjustment.");
    } else if (action === "continue-download") {
      setState("download-ready");
    } else if (action === "start-download") {
      setState("download-running", "Library build started. No action is needed.");
    } else if (action === "stop-safely") {
      announce("Stop requested. The current song will finish safely.");
    } else if (action === "open-folder") {
      announce("Prototype only. The Songs folder would open here.");
    } else if (action === "retry-missing") {
      announce("Retry queued for " + number(fixtures.done.partialMissing) + " songs.");
    } else if (action === "back-to-charts") {
      model.selectionMade = true;
      render();
      setState("charts-found");
    }
  });

  app.addEventListener("change", function (event) {
    const checkbox = event.target.closest("input[data-row-id]");
    if (!checkbox) return;
    model.rowSelection[checkbox.dataset.rowId] = checkbox.checked;
    render();
    announce(checkbox.checked ? "Chart added to the selection." : "Chart removed from the selection.");
  });

  app.addEventListener("input", function (event) {
    if (event.target.id !== "chart-search") return;
    model.search = event.target.value;
    render();
    const search = document.getElementById("chart-search");
    if (search) {
      search.focus();
      search.setSelectionRange(model.search.length, model.search.length);
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && (model.overlay || model.formatOpen)) {
      model.overlay = null;
      model.formatOpen = false;
      render();
    }
  });

  render();
}());
