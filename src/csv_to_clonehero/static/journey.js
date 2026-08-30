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
    controller: null,
    opener: null,
    formatOpen: false
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
    return shell('<section class="ready-stage stage-section"><div class="ready-copy"><p class="overline">Music ready</p><h1><span class="hero-number">' + number(music.canonical_count) + '</span><span class="hero-unit">songs</span></h1><p class="lead-copy">' + number(music.canonical_count) + ' unique songs after duplicates were removed.</p>' + attention + '<div class="source-summary" aria-label="Music source summary">' + sourceSummary(spotify, music.spotify_count) + sourceSummary(soundcloud, music.soundcloud_count) + '<div class="source-summary-row shared-row"><span class="source-summary-name"><span class="shared-mark" aria-hidden="true">+</span>In both</span><strong>' + number(music.shared_count) + '</strong><span>songs</span></div></div><div class="ready-actions"><div class="button-row"><button class="button primary" data-action="find-charts" disabled aria-disabled="true" title="Chart matching is not part of this slice">Find Clone Hero charts ' + icons.arrow + '</button><div class="format-action"><button class="button secondary" data-action="toggle-format" aria-haspopup="dialog" aria-expanded="' + (model.formatOpen ? "true" : "false") + '">' + icons.download + ' Download my song list</button>' + (model.formatOpen ? formatChooser() : "") + '</div></div><button class="text-link" data-action="manage-sources">Manage music sources ' + icons.arrow + '</button></div></div><div class="ready-visual" aria-hidden="true"><div class="source-orbit orbit-spotify"><span>Spotify</span><strong>' + number(music.spotify_count) + '</strong></div><div class="source-orbit orbit-soundcloud"><span>SoundCloud</span><strong>' + number(music.soundcloud_count) + '</strong></div><div class="orbit-center">' + icons.vinyl + '<span>in one<br>place</span></div><div class="visual-spark spark-one"></div><div class="visual-spark spark-two"></div></div></section>', "Music ready");
  }

  function formatChooser() {
    return '<div class="format-popover" role="dialog" aria-modal="true" aria-labelledby="format-heading"><div class="format-popover-head"><div><p class="overline">Song list</p><h2 id="format-heading">Choose a format</h2></div><button class="icon-button" data-action="close-format" aria-label="Close format chooser">' + icons.close + '</button></div><div class="format-options"><button class="format-option" data-action="choose-format" data-format="txt"><span class="format-type">TXT</span><span><strong>Artist - Song</strong><small>Plain text, easy to read</small></span>' + icons.arrow + '</button><button class="format-option" data-action="choose-format" data-format="csv"><span class="format-type">CSV</span><span><strong>Structured music list</strong><small>One song per row</small></span>' + icons.arrow + '</button><button class="format-option" data-action="choose-format" data-format="json"><span class="format-type">JSON</span><span><strong>Structured list with sources</strong><small>Includes source information</small></span>' + icons.arrow + '</button></div></div>';
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
      body = '<p class="sheet-lead">Your imported music is part of this library.</p><div class="sheet-stat"><strong>' + number(source.song_count) + '</strong><span>songs imported</span></div><p class="sheet-updated">Last updated ' + escapeHtml(date) + '</p>' + (source.state === "attention" ? '<div class="attention-note"><span class="attention-symbol">!</span><span><strong>Reconnect to update</strong><small>Your imported songs are still here.</small></span></div>' : "") + (soundcloud ? '<label class="file-picker"><span>Choose a replacement export</span><input id="soundcloud-file" type="file" accept=".csv,.json,text/csv,application/json"></label>' : "") + '<div class="sheet-actions">' + (spotify ? spotifyActions : '<button class="button primary" data-action="import-soundcloud">Replace import</button>') + '<button class="button quiet" data-action="view-imported">View imported music</button></div>';
    } else if (spotify) {
      body = '<p class="sheet-lead">Bring your listening history into this music library.</p><div class="sheet-preview"><span class="preview-mark">' + icons.sparkle + '</span><div><strong>Ready to bring it in</strong><small>Liked songs and saved albums stay in your library.</small></div></div><div class="sheet-actions"><button class="button primary" data-action="' + (source.connected ? 'update-spotify' : 'reconnect-source') + '">' + (source.connected ? 'Add Spotify music' : 'Connect Spotify') + '</button></div>';
    } else {
      body = '<p class="sheet-lead">Bring this source into your music library.</p><div class="sheet-preview"><span class="preview-mark">' + icons.sparkle + '</span><div><strong>Import a CSV or JSON export</strong><small>Your existing music stays safe if a replacement fails.</small></div></div><label class="file-picker"><span>Choose SoundCloud export</span><input id="soundcloud-file" type="file" accept=".csv,.json,text/csv,application/json"></label><div class="sheet-actions"><button class="button primary" data-action="import-soundcloud">Import SoundCloud music</button></div>';
    }
    return '<div class="sheet-backdrop" data-action="backdrop"><aside class="source-sheet" role="dialog" aria-modal="true" aria-labelledby="source-sheet-heading"><header class="sheet-head"><div class="sheet-source-title"><span class="source-mark ' + escapeHtml(info.art) + '" aria-hidden="true"><span>' + escapeHtml(info.mark) + '</span></span><div><p class="overline">Music source</p><h2 id="source-sheet-heading">' + escapeHtml(source.label) + '</h2><div class="sheet-status">' + statusCue(source) + '</div></div></div><button class="icon-button" data-action="close-sheet" aria-label="Close source sheet">' + icons.close + '</button></header><div class="sheet-body">' + body + '</div></aside></div>';
  }

  function render(focusOverlay) {
    const snapshot = model.snapshot;
    if (!snapshot) return;
    let content = snapshot.music.canonical_count ? musicReady(snapshot) : sourcesEmpty(snapshot);
    const stage = snapshot.operation.state === "importing" ? "Add music" : snapshot.music.canonical_count ? "Music ready" : "Add music";
    app.innerHTML = '<header class="topbar"><div class="brand"><span class="brand-mark" aria-hidden="true"><i></i><i></i><i></i></span><span class="brand-name">Riff</span><span class="brand-divider" aria-hidden="true"></span><span class="brand-context">music to Clone Hero</span></div><div class="stage-label"><span class="stage-pip" aria-hidden="true"></span><span id="stage-label">' + stage + '</span></div></header>' + content.slice(content.indexOf("<main"));
    app.setAttribute("aria-busy", snapshot.operation.state === "importing" ? "true" : "false");
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
      const wasImporting = model.snapshot && model.snapshot.operation.state === "importing";
      model.snapshot = snapshot;
      render(false);
      if (wasImporting && snapshot.operation.state !== "importing") announce("Your music import finished.");
      if (snapshot.operation.state === "importing") {
        clearTimeout(model.pollTimer);
        model.pollTimer = setTimeout(refresh, 600);
      }
    } catch (error) {
      if (error.name === "AbortError") return;
      announce(error.message);
      if (!model.snapshot) {
        document.getElementById("main-content").innerHTML = '<section class="error-stage stage-section"><p class="overline">Your music</p><h1>We could not load your library.</h1><p class="lead-copy">' + escapeHtml(error.message) + '</p><button class="button secondary" data-action="retry">Try again</button></section>';
      }
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
      await api("/api/spotify/inventory", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ workspace_id: model.snapshot.workspace_id }) });
      for (let attempt = 0; attempt < 100; attempt += 1) {
        const status = await api("/api/spotify/status?workspace_id=" + encodeURIComponent(model.snapshot.workspace_id));
        if (!status.running) {
          if (status.status === "failed") throw new Error(status.error || "Spotify update failed.");
          break;
        }
        await new Promise(function (resolve) { window.setTimeout(resolve, 300); });
      }
      await api("/api/spotify/import", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ workspace_id: model.snapshot.workspace_id }) });
      announce("Spotify music update started.");
      await refresh();
    } catch (error) { announce(error.message); }
    model.pendingAction = "";
  }

  app.addEventListener("click", function (event) {
    const backdrop = event.target.closest(".sheet-backdrop");
    if (backdrop && event.target === backdrop) { model.overlay = null; render(false); return; }
    const target = event.target.closest("[data-action]");
    if (!target || !app.contains(target)) return;
    const action = target.dataset.action;
    if (action === "open-source") { model.overlay = target.dataset.source; model.opener = target; model.formatOpen = false; render(true); }
    else if (action === "close-sheet" || action === "backdrop") { model.overlay = null; render(false); if (model.opener) model.opener.focus(); }
    else if (action === "toggle-format") { model.formatOpen = !model.formatOpen; render(true); }
    else if (action === "close-format") { model.formatOpen = false; render(false); if (model.opener) model.opener.focus(); }
    else if (action === "choose-format") { window.location.assign(apiUrl("/api/library/export?format=" + encodeURIComponent(target.dataset.format))); }
    else if (action === "manage-sources") { model.opener = target; model.overlay = model.snapshot.sources.spotify.imported || model.snapshot.sources.spotify.state === "attention" ? "spotify" : "soundcloud"; render(true); }
    else if (action === "import-soundcloud") { importSoundCloud(); }
    else if (action === "update-spotify") { updateSpotify(); }
    else if (action === "reconnect-source") { window.location.assign(apiUrl("/api/auth/spotify/start")); }
    else if (action === "view-imported") { model.overlay = null; render(false); document.getElementById("main-content").focus(); }
    else if (action === "find-charts") { announce("Chart matching will be available in the next integration slice."); }
    else if (action === "retry") { refresh(); }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && (model.overlay || model.formatOpen)) {
      event.preventDefault();
      const opener = model.opener;
      model.overlay = null;
      model.formatOpen = false;
      render(false);
      if (opener) opener.focus();
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
