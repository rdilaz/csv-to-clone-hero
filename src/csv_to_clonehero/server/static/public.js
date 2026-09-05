(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const authenticated = document.body.dataset.authenticated === "true";
  const app = $("#riffApp");
  const state = {
    journey: null,
    scope: null,
    scopeRequest: 0,
    jobId: null,
    pollTimer: null,
    progressAnnouncement: "",
    activityOffset: 0,
    activityTotal: 0,
    activityRequest: 0,
    reviewOffset: 0,
    reviewTotal: 0,
    reviewRequest: 0,
    reviewFilter: "recommended",
    reviewSearch: "",
    selected: 0,
    exactSelected: 0,
    exactSelectedSongs: 0,
    exactSelectionKnown: false,
    exactSelectedSongsKnown: false,
    expansionSelected: 0,
    expansionSelectionKnown: false,
    expansionJobId: null,
    expansionPollTimer: null,
    expansionPolling: false,
    expansionRequest: 0,
    expansionOffset: 0,
    expansionCards: [],
    selectionMode: "none",
    selectionBusy: false,
    selectionStale: false,
    selectionTotalKnown: false,
    openDisclosures: new Set(),
    closedDisclosures: new Set(),
  };

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
    headers.set("X-Riff-Request", "1");
    const response = await fetch(path, {...options, headers, credentials: "same-origin"});
    const data = await response.json().catch(() => ({}));
    if (response.status === 401) {
      window.location.assign("/");
      throw new Error("Your session ended. Sign in again to continue.");
    }
    if (!response.ok) {
      const error = new Error(data.error || data.preflight?.message || `Request failed (${response.status})`);
      error.data = data;
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, char => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;"})[char]);
  }

  function announce(message) {
    const node = $("#announcer");
    if (!node) return;
    node.textContent = "";
    window.setTimeout(() => { node.textContent = message; }, 20);
  }

  function showError(message) {
    const node = $("#appError");
    if (!node) return;
    node.textContent = message;
    node.hidden = false;
    node.focus?.();
  }

  function clearError() {
    const node = $("#appError");
    if (node) node.hidden = true;
  }

  function availableNumber(source, names) {
    for (const name of names) {
      const raw = source?.[name];
      if (raw === undefined || raw === null || raw === "") continue;
      const value = Number(raw);
      if (Number.isFinite(value) && value >= 0) return value;
    }
    return null;
  }

  function clearSelectionFeedback() {
    const message = $("#selectionMessage");
    const error = $("#selectionError");
    if (message) message.textContent = "";
    if (error) error.hidden = true;
  }

  function showSelectionError(message) {
    const node = $("#selectionError");
    if (!node) return;
    node.textContent = message;
    node.hidden = false;
  }

  function setButtonBusy(button, busy, busyText) {
    if (!button) return;
    if (button._riffLabel === undefined) button._riffLabel = button.innerHTML;
    button.disabled = busy;
    if (!button.querySelector("[id]")) {
      if (busy) button.textContent = busyText;
      else button.innerHTML = button._riffLabel;
    } else {
      if (button._riffAriaLabel === undefined) button._riffAriaLabel = button.getAttribute("aria-label");
      if (busy) button.setAttribute("aria-label", busyText || "Working");
      else if (button._riffAriaLabel === null) button.removeAttribute("aria-label");
      else button.setAttribute("aria-label", button._riffAriaLabel);
    }
    button.setAttribute("aria-busy", String(busy));
  }

  function selectionControls() {
    return $$('[data-selection], [data-expansion-selection], #selectBestButton, #selectAllButton, #selectMaximumButton, #clearSelectionButton');
  }

  function syncSelectionControls() {
    selectionControls().forEach(control => {
      if (control.dataset.riffOriginallyDisabled === undefined) {
        control.dataset.riffOriginallyDisabled = String(control.disabled);
      }
      const originallyDisabled = control.dataset.riffOriginallyDisabled === "true";
      control.disabled = originallyDisabled || state.selectionBusy || state.selectionStale;
      if (!state.selectionBusy && !state.selectionStale) {
        delete control.dataset.riffOriginallyDisabled;
      }
    });
  }

  function clearSelectionStale() {
    state.selectionStale = false;
    const host = $("#reviewCards");
    host?.removeAttribute("data-stale");
    const retry = $("#retrySelectionButton");
    if (retry) retry.hidden = true;
    syncSelectionControls();
  }

  function markSelectionStale(message) {
    state.selectionStale = true;
    const host = $("#reviewCards");
    host?.setAttribute("data-stale", "true");
    const retry = $("#retrySelectionButton");
    if (retry) retry.hidden = false;
    showSelectionError(`${message} Riff could not refresh the selection summary. Refresh it before continuing.`);
    syncSelectionControls();
    refreshSelectionUi();
  }

  async function logout() {
    try { await api("/api/auth/logout", {method: "POST", body: "{}"}); } catch (_) { /* redirect regardless */ }
    window.location.assign("/");
  }

  async function uploadSoundCloud(form, messageNode) {
    const input = $("input[type=file]", form);
    const button = $("button[type=submit]", form);
    if (!input?.files?.[0]) {
      messageNode.textContent = "Choose a CSV or JSON export first.";
      messageNode.classList.add("is-error");
      return null;
    }
    messageNode.classList.remove("is-error");
    messageNode.textContent = "Importing your SoundCloud music...";
    setButtonBusy(button, true, "Importing...");
    try {
      const body = new FormData();
      body.append("file", input.files[0]);
      const result = await api("/api/sources/soundcloud/import", {method: "POST", body});
      const count = Number(result.source?.tracks || 0).toLocaleString();
      messageNode.textContent = `${count} SoundCloud songs imported.`;
      announce(messageNode.textContent);
      return result;
    } catch (error) {
      messageNode.textContent = error.message;
      messageNode.classList.add("is-error");
      return null;
    } finally {
      setButtonBusy(button, false);
    }
  }

  function bindGuest() {
    const form = $("#guestSoundcloudForm");
    if (!form) return;
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const result = await uploadSoundCloud(form, $("#guestSoundcloudMessage"));
      if (result) {
        const songs = Number(result.combined?.tracks || 0).toLocaleString();
        $("#guestSoundcloudMessage").textContent = `${songs} songs ready. Continue with Spotify to find charts.`;
      }
    });
  }

  const stages = ["add", "find", "confirm", "download", "done"];

  function showStage(name, focus = false) {
    const ids = {add: "stageAdd", find: "stageFind", confirm: "stageConfirm", download: "stageDownload", done: "stageDone"};
    $("#stageLoading").hidden = true;
    for (const [key, id] of Object.entries(ids)) $("#" + id).hidden = key !== name;
    const current = stages.indexOf(name);
    $$(".journey-steps li").forEach((item, index) => {
      item.classList.toggle("is-current", index === current);
      item.classList.toggle("is-complete", index < current);
      if (index === current) item.setAttribute("aria-current", "step");
      else item.removeAttribute("aria-current");
    });
    document.body.dataset.riffStage = name;
    syncSelectionBand();
    if (focus) {
      const heading = $("#" + ids[name] + " h1");
      heading?.setAttribute("tabindex", "-1");
      heading?.focus({preventScroll: false});
    }
  }

  function hasSpotifyScope(scope) {
    return Boolean(scope?.include_liked || scope?.playlist_ids?.length);
  }

  function isTerminal(job) {
    return ["complete", "partial", "failed", "cancelled", "interrupted"].includes(job?.status);
  }

  async function restore() {
    clearError();
    try {
      const journey = await api("/api/journey");
      state.journey = journey;
      renderSources(journey);
      if (journey.download?.url) {
        renderDone(journey.download, null);
        return;
      }
      if (["queued", "running"].includes(journey.job?.status) && journey.job?.kind === "expansion") {
        await openConfirm(false);
        if (!state.expansionPolling) attachExpansionJob(journey.job.id);
        return;
      }
      if (["queued", "running"].includes(journey.job?.status)) {
        attachJob(journey.job.id);
        return;
      }
      if (isTerminal(journey.job) || journey.review?.total > 0) {
        await openConfirm(false);
        if (journey.job?.kind !== "expansion" && ["failed", "partial", "cancelled", "interrupted"].includes(journey.job?.status)) {
          const copy = journey.job.status === "failed"
            ? "The last search did not finish. Saved results are shown below; you can search again."
            : "The last search was incomplete. Review saved results or search again for missing songs.";
          showError(copy);
        }
        return;
      }
      await openAddMusic(false, journey);
    } catch (error) {
      showStage("add");
      const choices = $("#spotifyChoices");
      if (choices) {
        choices.innerHTML = "<p>Your saved music could not be restored. Reload to try again.</p>";
        choices.setAttribute("aria-busy", "false");
      }
      const freshness = $("#spotifyFreshness");
      if (freshness) freshness.textContent = "Spotify data is temporarily unavailable.";
      showError(error.message || "Riff could not restore your library.");
    }
  }

  function spotifyLibraryFromJourney(journey) {
    if (journey?.spotify_library) return journey.spotify_library;
    const spotify = (journey?.sources || []).find(source => source.provider === "spotify");
    return {
      playlists: [],
      liked_count: spotify ? Number(spotify.liked || 0) : null,
      workspace: {scope: journey?.spotify_scope || null, soundcloud_scope: {}},
      cache_available: Boolean(spotify),
      cached: true,
      cache_source: spotify ? "latest_import" : "none",
      last_refreshed_at: spotify?.updated_at || null,
      known_unique_songs: spotify ? Number(spotify.unique_tracks || spotify.tracks || 0) : null,
      imported_scope: {known: false, playlist_ids: [], include_liked: false},
    };
  }

  async function openAddMusic(focus = true, journey = state.journey) {
    state.reviewRequest += 1;
    state.expansionRequest += 1;
    showStage("add", focus);
    try {
      if (!journey) journey = await api("/api/journey");
      state.journey = journey;
      const scope = spotifyLibraryFromJourney(journey);
      state.scope = scope;
      renderSources(journey);
      renderScope(scope);
      void loadSoundCloudAuth();
      if (!scope.cache_available) {
        window.setTimeout(() => { void refreshSpotify(true); }, 0);
      }
    } catch (error) {
      showError(error.message || "Your music could not be loaded. Reload to try again.");
    }
  }

  async function loadSoundCloudAuth() {
    const soundcloud = await api("/api/auth/soundcloud/status").catch(() => ({configured: false}));
    await renderSoundCloudAuth(soundcloud);
  }

  function renderSpotifyFreshness(payload) {
    const node = $("#spotifyFreshness");
    if (!node) return;
    if (!payload.cache_available) {
      node.textContent = "No saved playlist details yet. Refresh Spotify to load them.";
      return;
    }
    if (!payload.cached) {
      node.textContent = "Updated from Spotify just now.";
      return;
    }
    const updated = Number(payload.last_refreshed_at || 0);
    const prefix = payload.cache_source === "latest_import"
      ? "Showing Spotify data saved by your latest import."
      : "Showing saved Spotify data.";
    node.textContent = updated
      ? `${prefix} Last refreshed ${new Date(updated * 1000).toLocaleString()}.`
      : prefix;
  }

  async function refreshSpotify(automatic = false) {
    const button = $("#refreshSpotifyButton");
    const freshness = $("#spotifyFreshness");
    const requestId = ++state.scopeRequest;
    setButtonBusy(button, true, "Refreshing...");
    if (freshness) freshness.textContent = "Refreshing Spotify; saved choices remain available.";
    try {
      const payload = await api("/api/spotify/scope");
      if (requestId !== state.scopeRequest) return;
      const selected = $("#spotifyChoices")?.getAttribute("aria-busy") === "false"
        ? currentScope()
        : null;
      state.scope = payload;
      if (state.journey) state.journey.spotify_library = payload;
      renderScope(payload, selected);
      clearError();
      if (!automatic) announce("Spotify playlists refreshed.");
    } catch (error) {
      if (requestId !== state.scopeRequest) return;
      if (freshness) freshness.textContent = state.scope?.cache_available
        ? "Spotify could not be refreshed. Saved choices are still shown."
        : "Spotify could not be loaded. Use Refresh Spotify to try again.";
      if (!automatic) showError(error.message || "Spotify could not be refreshed.");
    } finally {
      if (requestId === state.scopeRequest) setButtonBusy(button, false);
    }
  }

  function renderScope(payload, selectedScope = null) {
    const host = $("#spotifyChoices");
    if (!host) return;
    const saved = selectedScope || payload.workspace?.scope || {};
    const selectedIds = new Set(saved.playlist_ids || []);
    const playlists = [...(payload.playlists || [])];
    const knownIds = new Set(playlists.map(item => item.id));
    for (const id of selectedIds) {
      if (!knownIds.has(id)) playlists.push({id, name: "Saved playlist", track_count: null});
    }
    const playlistRows = playlists.map(item => {
      const count = item.track_count === null || item.track_count === undefined ? null : Number(item.track_count);
      const displayCount = Number.isFinite(count) ? count.toLocaleString() : "Count unavailable";
      const tallyCount = Number.isFinite(count) ? String(count) : "";
      return `<label><input class="spotify-playlist" type="checkbox" value="${escapeHtml(item.id)}" data-track-count="${tallyCount}" ${selectedIds.has(item.id) ? "checked" : ""}><span>${escapeHtml(item.name || "Untitled playlist")}</span><small>${displayCount}</small></label>`;
    });
    const likedCount = payload.liked_count === null || payload.liked_count === undefined
      ? null
      : Number(payload.liked_count);
    const rows = [
      `<label><input id="includeLiked" type="checkbox" ${saved.include_liked ? "checked" : ""}><span>Liked Songs</span><small>${Number.isFinite(likedCount) ? likedCount.toLocaleString() : "Count unavailable"}</small></label>`,
      ...playlistRows,
    ];
    host.innerHTML = rows.join("") || "<p>No Spotify playlists were returned.</p>";
    host.setAttribute("aria-busy", "false");
    host.onchange = updateMusicTally;
    renderSpotifyFreshness(payload);
    updateMusicTally();
  }

  function renderSources(journey) {
    const sources = journey.sources || [];
    const spotify = sources.find(source => source.provider === "spotify");
    const imported = sources.find(source => source.provider === "soundcloud");
    const spotifySummary = $("#spotifyImportedSummary");
    if (spotifySummary) {
      const unique = Number(spotify?.unique_tracks || spotify?.tracks || 0);
      const playlists = Number(spotify?.playlists || 0);
      const raw = Number(spotify?.raw_items || 0);
      const liked = Number(spotify?.liked || 0);
      const playlistOnly = Number(spotify?.playlist_only || 0);
      const importedScope = journey.spotify_library?.imported_scope;
      const likedLabel = importedScope?.known && importedScope.include_liked
        ? " + Liked Songs"
        : "";
      const playlistLabel = `${playlists.toLocaleString()} ${playlists === 1 ? "playlist" : "playlists"}`;
      const sourceItemLabel = `${spotify?.raw_items_lower_bound ? "at least " : ""}${raw.toLocaleString()} selected source entries`;
      if (spotify?.status === "partial") {
        spotifySummary.textContent = unique || playlists
          ? `Partial import: ${playlistLabel}; ${sourceItemLabel} processed, with ${unique.toLocaleString()} unique ${unique === 1 ? "song" : "songs"} saved so far.`
          : "Partial Spotify import; refresh or import again for final counts.";
      } else {
        spotifySummary.textContent = unique || playlists
          ? `${playlistLabel}${likedLabel}; ${sourceItemLabel} became ${unique.toLocaleString()} unique ${unique === 1 ? "song" : "songs"} (${liked.toLocaleString()} liked, ${playlistOnly.toLocaleString()} playlist-only).`
          : "";
      }
    }
    const count = Number(imported?.unique_tracks || imported?.tracks || 0);
    const playlistCount = Number(imported?.playlists || 0);
    const badge = $("#soundcloudCount");
    if (badge) {
      badge.textContent = imported?.status === "partial"
        ? `Partial: ${count.toLocaleString()} unique ${count === 1 ? "song" : "songs"} saved`
        : count || playlistCount
          ? `${count.toLocaleString()} unique ${count === 1 ? "song" : "songs"}${playlistCount ? `, ${playlistCount.toLocaleString()} ${playlistCount === 1 ? "playlist" : "playlists"}` : ""}`
          : "Not added";
      badge.classList.toggle("status-pill--quiet", !(count || playlistCount));
    }
    updateMusicTally();
  }

  async function renderSoundCloudAuth(status) {
    const host = $("#soundcloudOauth");
    if (!host || !status.configured) return;
    host.hidden = false;
    if (!status.authenticated) return;
    try {
      const data = await api("/api/soundcloud/scope");
      const saved = state.scope?.workspace?.soundcloud_scope || {};
      const savedIds = new Set(saved.playlist_ids || []);
      host.innerHTML = `<p class="fine-print">SoundCloud is connected. Choose what to include.</p>
        <div class="choice-list">
          <label><input id="soundcloudLiked" type="checkbox" ${saved.include_liked ? "checked" : ""}><span>Liked Tracks</span><small>${Number(data.liked_count || 0).toLocaleString()}</small></label>
          ${(data.playlists || []).map(item => `<label><input class="soundcloud-playlist" type="checkbox" value="${escapeHtml(item.id)}" ${savedIds.has(String(item.id)) ? "checked" : ""}><span>${escapeHtml(item.title || item.name || "Untitled playlist")}</span></label>`).join("")}
        </div>
        <button class="button button--secondary" id="saveSoundcloudScope" type="button">Save SoundCloud selection</button>
        <p class="form-message" id="soundcloudScopeMessage" role="status" aria-live="polite"></p>`;
      $("#saveSoundcloudScope")?.addEventListener("click", saveSoundCloudScope);
    } catch (_) {
      host.innerHTML = "<p class=\"fine-print\">SoundCloud is connected, but its library could not be loaded. You can still use Spotify or upload an export.</p>";
    }
  }

  async function saveSoundCloudScope() {
    const button = $("#saveSoundcloudScope");
    const message = $("#soundcloudScopeMessage");
    setButtonBusy(button, true, "Saving...");
    try {
      const result = await api("/api/soundcloud/scope", {method: "POST", body: JSON.stringify({
        include_liked: Boolean($("#soundcloudLiked")?.checked),
        playlist_ids: $$(".soundcloud-playlist:checked").map(input => input.value),
      })});
      if (state.scope?.workspace) state.scope.workspace.soundcloud_scope = result.scope;
      if (state.journey?.soundcloud_library) {
        state.journey.soundcloud_library.current_scope = result.scope;
      }
      updateMusicTally();
      message.textContent = "SoundCloud selection saved.";
      message.classList.remove("is-error");
      announce(message.textContent);
    } catch (error) {
      message.textContent = error.message;
      message.classList.add("is-error");
    } finally {
      setButtonBusy(button, false);
    }
  }

  function currentScope() {
    return {
      include_liked: Boolean($("#includeLiked")?.checked),
      playlist_ids: $$(".spotify-playlist:checked").map(input => input.value),
    };
  }

  function setAllSpotifyPlaylists(checked) {
    const playlists = $$(".spotify-playlist");
    playlists.forEach(input => { input.checked = checked; });
    updateMusicTally();
    announce(`${checked ? "Selected" : "Cleared"} ${playlists.length.toLocaleString()} Spotify playlists.`);
  }

  function scopesMatch(left, right) {
    if (!right?.known) return false;
    const leftIds = [...new Set(left?.playlist_ids || [])].sort();
    const rightIds = [...new Set(right.playlist_ids || [])].sort();
    return Boolean(left?.include_liked) === Boolean(right.include_liked)
      && leftIds.length === rightIds.length
      && leftIds.every((value, index) => value === rightIds[index]);
  }

  function updateMusicTally() {
    const tally = $("#musicTally");
    if (!tally) return;
    const selectedScope = currentScope();
    const likedValue = state.scope?.liked_count;
    const liked = selectedScope.include_liked && likedValue !== null && likedValue !== undefined
      ? Number(likedValue)
      : 0;
    let hasUnknownPlaylistCount = false;
    if (selectedScope.include_liked && (likedValue === null || likedValue === undefined)) {
      hasUnknownPlaylistCount = true;
    }
    const playlistCount = $$(".spotify-playlist:checked").reduce((sum, input) => {
      if (!input.dataset.trackCount) {
        hasUnknownPlaylistCount = true;
        return sum;
      }
      return sum + Number(input.dataset.trackCount);
    }, 0);
    const sources = state.journey?.sources || [];
    const spotify = sources.find(source => source.provider === "spotify");
    const soundcloud = sources.find(source => source.provider === "soundcloud");
    const spotifyUnique = Number(spotify?.unique_tracks || spotify?.tracks || state.scope?.known_unique_songs || 0);
    const soundcloudUnique = Number(soundcloud?.unique_tracks || soundcloud?.tracks || 0);
    const combinedUnique = Number(state.journey?.combined?.tracks || 0);
    const estimated = liked + playlistCount;
    const spotifyChosen = selectedScope.include_liked || selectedScope.playlist_ids.length > 0;
    const importedScopeMatches = scopesMatch(selectedScope, state.scope?.imported_scope);
    const soundcloudLibrary = state.journey?.soundcloud_library;
    const currentSoundcloudScope = soundcloudLibrary?.current_scope || {};
    const soundcloudScopeSelected = Boolean(
      currentSoundcloudScope.include_liked || currentSoundcloudScope.playlist_ids?.length
    );
    const soundcloudScopeRelevant = Boolean(soundcloudUnique || soundcloudScopeSelected);
    const soundcloudScopeMatches = !soundcloudScopeRelevant || Boolean(
      soundcloudLibrary?.status !== "partial"
      && scopesMatch(currentSoundcloudScope, soundcloudLibrary?.imported_scope)
    );
    const strong = tally.querySelector("strong");
    const small = tally.querySelector("small");
    if (importedScopeMatches && soundcloudScopeMatches && (spotifyUnique || soundcloudUnique || combinedUnique)) {
      const latestTotal = combinedUnique || spotifyUnique || soundcloudUnique;
      strong.textContent = `${latestTotal.toLocaleString()} combined unique ${latestTotal === 1 ? "song" : "songs"} in the latest import`;
      const sourceCounts = [];
      if (spotifyUnique) sourceCounts.push(`${spotifyUnique.toLocaleString()} unique Spotify ${spotifyUnique === 1 ? "song" : "songs"}`);
      if (soundcloudUnique) sourceCounts.push(`${soundcloudUnique.toLocaleString()} unique SoundCloud ${soundcloudUnique === 1 ? "song" : "songs"}`);
      small.textContent = `${sourceCounts.join(" and ")}${sourceCounts.length ? "; " : ""}${latestTotal.toLocaleString()} combined after cross-provider deduplication.`;
    } else if (spotifyChosen || hasUnknownPlaylistCount) {
      strong.textContent = hasUnknownPlaylistCount
        ? estimated ? `At least ${estimated.toLocaleString()} selected Spotify ${estimated === 1 ? "entry" : "entries"}` : "Spotify sources selected"
        : `${estimated.toLocaleString()} selected Spotify ${estimated === 1 ? "entry" : "entries"}`;
      small.textContent = "Playlist memberships and Liked Songs can overlap. The exact unique total will be determined after import and cross-provider deduplication."
        + (soundcloudUnique
          ? soundcloud?.status === "partial"
            ? ` ${soundcloudUnique.toLocaleString()} unique SoundCloud ${soundcloudUnique === 1 ? "song is" : "songs are"} saved from a partial import.`
            : ` ${soundcloudUnique.toLocaleString()} unique SoundCloud ${soundcloudUnique === 1 ? "song is" : "songs are"} already imported.`
          : "");
    } else if (soundcloudUnique) {
      const soundcloudPartial = soundcloud?.status === "partial";
      strong.textContent = soundcloudPartial
        ? `${soundcloudUnique.toLocaleString()} unique SoundCloud ${soundcloudUnique === 1 ? "song" : "songs"} saved so far`
        : `${soundcloudUnique.toLocaleString()} unique SoundCloud ${soundcloudUnique === 1 ? "song" : "songs"} imported`;
      small.textContent = soundcloudPartial
        ? "The SoundCloud import is partial; its final unique and combined totals are not known yet."
        : combinedUnique && combinedUnique !== soundcloudUnique
          ? `${combinedUnique.toLocaleString()} combined unique songs remain in the latest imported library.`
          : "Choose Spotify music to add it before cross-provider deduplication.";
    } else {
      strong.textContent = "Choose music above";
      small.textContent = "Liked Songs, playlists, or an optional SoundCloud export.";
    }
  }

  async function importAuthenticatedSoundCloud(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const result = await uploadSoundCloud(form, $("#soundcloudMessage"));
    if (!result) return;
    state.journey.sources = result.sources || [];
    state.journey.combined = result.combined || {};
    state.journey.soundcloud_library = result.soundcloud_library || {
      status: "partial",
      current_scope: state.scope?.workspace?.soundcloud_scope || {},
      imported_scope: {known: false, playlist_ids: [], include_liked: false},
    };
    renderSources(state.journey);
  }

  async function saveScopeIfChosen() {
    const scope = currentScope();
    if (!hasSpotifyScope(scope)) return false;
    await api("/api/spotify/scope", {method: "POST", body: JSON.stringify(scope)});
    return true;
  }

  async function ensurePreflight() {
    let response = await api("/api/preflight");
    if (!response.preflight?.usable) response = await api("/api/preflight", {method: "POST", body: "{}"});
    if (!response.preflight?.usable) throw new Error(response.preflight?.message || "The community chart library is unavailable right now.");
  }

  async function startDiscovery() {
    const button = $("#findChartsButton");
    clearError();
    setButtonBusy(button, true, "Preparing search...");
    try {
      const spotifyChosen = await saveScopeIfChosen();
      const imported = Number(state.journey?.combined?.tracks || 0) > 0;
      if (!spotifyChosen && !imported) throw new Error("Choose Liked Songs, a playlist, or import SoundCloud music first.");
      state.reviewRequest += 1;
      state.expansionRequest += 1;
      state.reviewOffset = 0;
      state.reviewSearch = "";
      state.reviewFilter = "recommended";
      state.exactSelected = 0;
      state.exactSelectedSongs = 0;
      state.exactSelectionKnown = false;
      state.exactSelectedSongsKnown = false;
      state.expansionSelected = 0;
      state.expansionSelectionKnown = !$("#expansionPanel");
      state.selectionTotalKnown = false;
      state.selectionStale = false;
      state.selected = 0;
      state.selectionMode = "none";
      state.openDisclosures.clear();
      state.closedDisclosures.clear();
      $("#reviewCards")?.replaceChildren();
      clearSelectionFeedback();
      clearSelectionStale();
      if ($("#reviewSearch")) $("#reviewSearch").value = "";
      $$('[data-review-filter]').forEach(item => item.setAttribute("aria-pressed", String(item.dataset.reviewFilter === "recommended")));
      resetFindUi();
      showStage("find", true);
      $("#progressMessage").textContent = "Checking the community chart library...";
      await ensurePreflight();
      const result = await api("/api/jobs", {method: "POST", body: JSON.stringify({kind: "discovery"})});
      attachJob(result.job.id);
    } catch (error) {
      if (error.data?.job?.id) {
        attachJob(error.data.job.id);
        return;
      }
      showStage("add");
      showError(error.message);
    } finally {
      setButtonBusy(button, false);
    }
  }

  function resetFindUi() {
    state.activityOffset = 0;
    state.activityTotal = 0;
    state.activityRequest += 1;
    state.progressAnnouncement = "";
    $("#progressDisc")?.classList.add("is-indeterminate");
    $("#progressDisc")?.removeAttribute("aria-valuenow");
    if ($("#progressPercent")) $("#progressPercent").textContent = "Working...";
    if ($("#progressBar")) $("#progressBar").style.width = "0";
    if ($("#progressCount")) $("#progressCount").textContent = "Preparing your music";
    if ($("#currentSong")) $("#currentSong").textContent = "Waiting for the first song...";
    if ($("#activityList")) $("#activityList").innerHTML = "";
  }

  function attachJob(jobId) {
    const expectedJobId = Number(jobId);
    state.jobId = expectedJobId;
    showStage("find");
    if (state.pollTimer) window.clearTimeout(state.pollTimer);
    state.pollTimer = null;
    void pollJob(expectedJobId);
  }

  function progressNumbers(job) {
    const progress = job.progress || {};
    const pairs = [
      [progress.processed_tracks, progress.total_tracks, "songs"],
      [progress.completed_tracks, progress.total_tracks, "songs"],
      [progress.completed_work, progress.total_work, "checks"],
      [progress.scoring_tasks_completed, progress.scoring_tasks_total, "checks"],
      [progress.completed_tasks, progress.total_tasks, "queries"],
    ];
    for (const [rawDone, rawTotal, unit] of pairs) {
      if (rawDone === undefined || rawDone === null || rawDone === "") continue;
      const total = Number(rawTotal || 0);
      if (!Number.isFinite(total) || total <= 0) continue;
      const value = Number(rawDone || 0);
      const done = Math.max(0, Math.min(total, Number.isFinite(value) ? value : 0));
      const calculated = Math.min(100, Math.round(done / total * 100));
      const percent = !isTerminal(job) && calculated === 100 ? 99 : calculated;
      return {progress, total, done, percent, unit};
    }
    return {progress, total: 0, done: 0, percent: 0, unit: "checks"};
  }

  function firstFinite(sources, names) {
    for (const source of sources) {
      for (const name of names) {
        const raw = source?.[name];
        if (raw === undefined || raw === null || raw === "") continue;
        const value = Number(raw);
        if (Number.isFinite(value) && value >= 0) return value;
      }
    }
    return null;
  }

  function elapsedSeconds(job, progress) {
    const direct = firstFinite([progress, job], ["elapsed_seconds", "elapsed"]);
    if (direct !== null) return direct;
    for (const source of [progress, job]) {
      for (const name of ["started_at", "start_time", "start_timestamp"]) {
        const raw = source?.[name];
        if (raw === undefined || raw === null || raw === "") continue;
        const numeric = Number(raw);
        const started = Number.isFinite(numeric)
          ? numeric / (numeric > 1e12 ? 1000 : 1)
          : Date.parse(String(raw)) / 1000;
        const elapsed = Date.now() / 1000 - started;
        if (Number.isFinite(elapsed) && elapsed >= 0) return elapsed;
      }
    }
    return null;
  }

  function remainingSeconds(value) {
    if (value === undefined || value === null || value === "") return null;
    const numeric = Number(value);
    const timestamp = Number.isFinite(numeric) ? numeric : Date.parse(String(value));
    if (!Number.isFinite(timestamp) || timestamp < 0) return null;
    const seconds = !Number.isFinite(numeric) || numeric > 1e9
      ? timestamp / (timestamp > 1e12 ? 1000 : 1) - Date.now() / 1000
      : timestamp;
    return Number.isFinite(seconds) && seconds >= 0 ? Math.min(seconds, 30 * 86400) : null;
  }

  function firstRemaining(sources, names) {
    for (const source of sources) {
      for (const name of names) {
        const value = remainingSeconds(source?.[name]);
        if (value !== null) return value;
      }
    }
    return null;
  }

  function roundedMinutes(seconds) {
    const minutes = Math.max(1, seconds / 60);
    if (minutes < 10) return Math.max(1, Math.round(minutes));
    if (minutes < 30) return Math.round(minutes / 5) * 5;
    return Math.round(minutes / 10) * 10;
  }

  function roundedHours(seconds) {
    const hours = Math.max(1, seconds / 3600);
    if (hours < 3) return Math.round(hours * 2) / 2;
    if (hours < 12) return Math.round(hours);
    return Math.round(hours / 2) * 2;
  }

  function friendlyEta(lower, upper = lower) {
    lower = Math.max(0, Math.min(lower, 30 * 86400));
    upper = Math.max(lower, Math.min(upper, 30 * 86400));
    if (upper <= 45) return "Less than a minute";
    const materialRange = upper - lower >= Math.max(60, ((lower + upper) / 2) * .2);
    if (upper < 5400) {
      const low = roundedMinutes(lower);
      const high = roundedMinutes(upper);
      if (materialRange && high > low) return `About ${low}-${high} minutes`;
      const minutes = roundedMinutes((lower + upper) / 2);
      return minutes === 1 ? "About a minute" : `About ${minutes} minutes`;
    }
    const low = roundedHours(lower);
    const high = roundedHours(upper);
    if (materialRange && high > low) return `About ${low}-${high} hours`;
    const hours = roundedHours((lower + upper) / 2);
    return hours === 1 ? "About an hour" : `About ${hours} hours`;
  }

  function progressEta(job, progress, total, done) {
    if (!total || done < 3) return "Estimating...";
    if (done >= total) return "Almost done";

    const sources = [progress, job];
    const elapsed = elapsedSeconds(job, progress);
    const providedRate = firstFinite(sources, ["observed_songs_per_second", "completion_rate", "rate", "tracks_per_second", "songs_per_second", "items_per_second"]);
    let lower = firstRemaining(sources, ["eta_lower_seconds", "eta_lower", "eta_seconds_lower"]);
    let upper = firstRemaining(sources, ["eta_upper_seconds", "eta_upper", "eta_seconds_upper"]);
    const estimatedCompleted = firstFinite(sources, ["estimated_completed_seconds"]);
    const estimatedLower = firstRemaining(sources, ["estimated_lower_seconds"]);
    const estimatedUpper = firstRemaining(sources, ["estimated_upper_seconds"]);
    if (lower === null && estimatedLower !== null) lower = Math.max(0, estimatedLower - (estimatedCompleted || 0));
    if (upper === null && estimatedUpper !== null) upper = Math.max(0, estimatedUpper - (estimatedCompleted || 0));
    if ((elapsed === null || elapsed < 5) && !(providedRate > 0) && lower === null && upper === null) return "Estimating...";
    if (lower !== null || upper !== null) return friendlyEta(lower ?? upper, upper ?? lower);

    const observedRate = elapsed !== null && elapsed >= 5 ? done / elapsed : 0;
    const rate = observedRate > 0 ? observedRate : providedRate;
    if (!Number.isFinite(rate) || rate <= 0) return "Estimating...";
    const estimate = (total - done) / rate;
    if (!Number.isFinite(estimate) || estimate < 0) return "Estimating...";

    const uncertainty = Math.min(.5, Math.max(.08, 1 / Math.sqrt(done)));
    return friendlyEta(estimate * (1 - uncertainty), estimate * (1 + uncertainty));
  }

  async function pollJob(expectedJobId = state.jobId) {
    if (!expectedJobId || expectedJobId !== state.jobId) return;
    try {
      const {job} = await api(`/api/jobs/${expectedJobId}`);
      if (expectedJobId !== state.jobId) return;
      renderProgress(job);
      if ($("#activityDetails")?.open) loadActivity();
      if (isTerminal(job)) {
        window.clearTimeout(state.pollTimer);
        state.pollTimer = null;
        state.journey = await api("/api/journey");
        if (expectedJobId !== state.jobId) return;
        if (job.status === "failed") showError("The search stopped before it finished. Your completed work is saved; start again when ready.");
        else if (job.status !== "complete") showError("The search was interrupted. Saved results are available, but some songs may still need checking.");
        await openConfirm(true);
        return;
      }
    } catch (error) {
      if (expectedJobId !== state.jobId) return;
      window.clearTimeout(state.pollTimer);
      state.pollTimer = null;
      showError(error.message);
      return;
    }
    if (expectedJobId === state.jobId) {
      state.pollTimer = window.setTimeout(() => { void pollJob(expectedJobId); }, 1500);
    }
  }

  function renderProgress(job) {
    const {progress, total, done, percent, unit} = progressNumbers(job);
    const disc = $("#progressDisc");
    disc.classList.toggle("is-indeterminate", !total);
    if (total) disc.setAttribute("aria-valuenow", String(percent));
    else disc.removeAttribute("aria-valuenow");
    disc.setAttribute("aria-valuetext", total ? `${done} of ${total} ${unit} complete` : "Preparing music");
    $("#progressPercent").textContent = total ? `${percent}%` : "Working...";
    $("#progressBar").style.width = `${percent}%`;
    const countLabel = unit === "songs" ? "songs checked" : `${unit} completed`;
    $("#progressCount").textContent = total ? `${done.toLocaleString()} of ${total.toLocaleString()} ${countLabel}` : "Preparing your music";
    $("#progressEta").textContent = progressEta(job, progress, total, done);
    const phases = {
      queued: "Your search is waiting its turn. Riff searches carefully to protect the community service.",
      starting: "Preparing your selected music...",
      provider_import: "Loading your selected music...",
      combine: "Merging duplicate songs...",
      catalog_prepare: "Preparing the community chart catalog...",
      finding_charts: "Comparing songs with playable community charts...",
      spotify_sync: "Loading your selected music...",
      source_search: "Checking the community chart library...",
      scoring: "Comparing songs with playable community charts...",
    };
    const phaseMessage = phases[job.status === "queued" ? "queued" : job.phase] || job.message || "Finding charts...";
    $("#progressMessage").textContent = phaseMessage;
    const current = progress.current_song;
    $("#currentSong").textContent = typeof current === "string" ? current : current?.artist && current?.title ? `${current.artist} - ${current.title}` : "Waiting for the next song...";
    const milestone = total ? `${job.phase || job.status}:${Math.floor(percent / 25)}` : `${job.phase || job.status}:working`;
    if (milestone !== state.progressAnnouncement) {
      state.progressAnnouncement = milestone;
      announce(total ? `${phaseMessage} ${percent}% complete.` : phaseMessage);
    }
  }

  async function loadActivity() {
    if (!state.jobId) return;
    const requestId = ++state.activityRequest;
    const expectedJobId = state.jobId;
    const params = new URLSearchParams({limit: "50", offset: String(state.activityOffset)});
    const search = $("#activitySearch")?.value.trim();
    if (search) params.set("search", search);
    const data = await api(`/api/jobs/${expectedJobId}/songs?${params}`);
    if (requestId !== state.activityRequest || expectedJobId !== state.jobId) return;
    state.activityTotal = Number(data.total || 0);
    $("#activityList").innerHTML = (data.songs || []).map(song => `<li><strong>${escapeHtml(song.artist)} - ${escapeHtml(song.title)}</strong><span>${escapeHtml(activityLabel(song.classification || song.status))}</span></li>`).join("") || "<li>No song activity yet.</li>";
    const page = Math.floor(state.activityOffset / 50) + 1;
    $("#activityPage").textContent = `${page} / ${Math.max(1, Math.ceil(state.activityTotal / 50))}`;
    $("#activityPrev").disabled = state.activityOffset === 0;
    $("#activityNext").disabled = state.activityOffset + 50 >= state.activityTotal;
  }

  function activityLabel(value) {
    return ({waiting: "Waiting", checking: "Checking", strong: "Best match", review: "Review", no_suitable: "No suitable chart", not_playable: "Not playable", no_catalog_candidate: "No chart found"})[value] || value || "Waiting";
  }

  async function cancelDiscovery() {
    if (!state.jobId || !window.confirm("Cancel this search? Completed progress will remain saved.")) return;
    try { await api(`/api/jobs/${state.jobId}/cancel`, {method: "POST", body: "{}"}); }
    catch (error) { showError(error.message); }
  }

  async function openConfirm(focus = true) {
    showStage("confirm", focus);
    state.reviewOffset = 0;
    if (!$("#expansionPanel")) {
      state.expansionSelected = 0;
      state.expansionSelectionKnown = true;
    }
    await Promise.all([loadReview(), loadExpansion()]);
    syncSelectionBand();
  }

  function reviewParams() {
    const params = new URLSearchParams({limit: "25", offset: String(state.reviewOffset)});
    if (state.reviewSearch) params.set("search", state.reviewSearch);
    if (state.reviewFilter === "recommended") params.set("tier", "RECOMMENDED_EXACT,SAME_RECORDING_CHART_VARIANT,ALTERNATE_RECORDING,POSSIBLE_MATCH");
    if (state.reviewFilter === "strong") params.set("tier", "RECOMMENDED_EXACT");
    if (state.reviewFilter === "review") params.set("tier", "POSSIBLE_MATCH");
    if (state.reviewFilter === "selected") params.set("selected", "true");
    return params;
  }

  function captureReviewUi() {
    const active = document.activeElement;
    return {
      focusKey: active?.dataset?.focusKey || "",
      scrollX: window.scrollX,
      scrollY: window.scrollY,
    };
  }

  function restoreReviewUi(snapshot, preferredFocusKey = "") {
    const focusKey = preferredFocusKey || snapshot.focusKey;
    if (!focusKey) return;
    const target = $$('[data-focus-key]').find(node => node.dataset.focusKey === focusKey);
    if (target) {
      target.focus({preventScroll: true});
      window.scrollTo({left: snapshot.scrollX, top: snapshot.scrollY, behavior: "auto"});
      return;
    }
    if (preferredFocusKey) {
      const fallback = [$("#reviewSearch"), $('[data-review-filter][aria-pressed="true"]'), $(".song-group__identity h3")]
        .find(node => node && node.getClientRects().length);
      if (fallback && !fallback.matches('a, button, input, select, textarea, [tabindex]')) fallback.setAttribute("tabindex", "-1");
      fallback?.focus({preventScroll: true});
    }
  }

  async function loadReview(options = {}) {
    const requestId = ++state.reviewRequest;
    const snapshot = captureReviewUi();
    const host = $("#reviewCards");
    host?.setAttribute("aria-busy", "true");
    try {
      const data = await api(`/api/workspace?${reviewParams()}`);
      if (requestId !== state.reviewRequest) return false;
      state.reviewTotal = Number(data.total || 0);
      const exactSelected = availableNumber(data.summary, ["selected_charts", "selection_count"]);
      const selectedSongs = availableNumber(data.summary, ["selected_tracks", "selected"]);
      if (exactSelected !== null) {
        state.exactSelected = exactSelected;
        state.exactSelectionKnown = true;
      } else state.exactSelectionKnown = false;
      if (selectedSongs !== null) {
        state.exactSelectedSongs = selectedSongs;
        state.exactSelectedSongsKnown = true;
      } else state.exactSelectedSongsKnown = false;
      const legacyRecommended = Number(data.summary?.strong || 0) + Number(data.summary?.review || 0);
      const recommended = Number(data.summary?.recommended_songs ?? legacyRecommended);
      const additional = Number(data.summary?.additional_playable_charts || 0);
      const selectable = Number(data.summary?.selectable_charts ?? legacyRecommended);
      $("#chartsFoundCount").textContent = recommended.toLocaleString();
      $("#chartsFoundLabel").textContent = recommended === 1 ? "song has a recommended chart." : "songs have recommended charts.";
      $("#bestMatchCount").textContent = recommended.toLocaleString();
      $("#allMatchCount").textContent = Number(data.summary?.all_safe_charts ?? recommended).toLocaleString();
      $("#maximumMatchCount").textContent = Number(data.summary?.maximum_playable_charts ?? selectable).toLocaleString();
      if (!options.preserveStale) {
        const wasStale = state.selectionStale;
        clearSelectionStale();
        if (wasStale) clearSelectionFeedback();
      }
      refreshSelectionUi();
      $("#reviewSummary").textContent = `${recommended.toLocaleString()} songs have recommendations; ${additional.toLocaleString()} additional playable charts are available.`;
      host.innerHTML = (data.tracks || []).map((track, index) => reviewCard(track, state.reviewOffset + index)).join("") || `<div class="empty-state">No recommendations match this view.</div>`;
      host.setAttribute("aria-busy", "false");
      bindReviewActions();
      const page = Math.floor(state.reviewOffset / 25) + 1;
      $("#reviewPage").textContent = `${page} / ${Math.max(1, Math.ceil(state.reviewTotal / 25))}`;
      $("#reviewPrev").disabled = state.reviewOffset === 0;
      $("#reviewNext").disabled = state.reviewOffset + 25 >= state.reviewTotal;
      restoreReviewUi(snapshot, options.focusKey || "");
      if (options.announcement) {
        const total = `${state.selected.toLocaleString()} ${state.selected === 1 ? "chart" : "charts"} selected.`;
        const message = state.selectionTotalKnown ? `${options.announcement} ${total}` : `${options.announcement} Updating the combined selection total.`;
        $("#selectionMessage").textContent = message;
        announce(message);
      }
      syncSelectionBand();
      return true;
    } catch (error) {
      if (requestId !== state.reviewRequest) return false;
      host?.setAttribute("aria-busy", "false");
      if (!options.reconciliation) showError(error.message || "Chart recommendations could not be refreshed.");
      return false;
    }
  }

  function refreshSelectionUi() {
    const exactConfirmed = state.exactSelectionKnown ? state.exactSelected : 0;
    const expansionConfirmed = state.expansionSelectionKnown ? state.expansionSelected : 0;
    state.selected = exactConfirmed + expansionConfirmed;
    state.selectionTotalKnown = state.exactSelectionKnown && state.expansionSelectionKnown && !state.selectionStale;
    $("#selectedCount").textContent = state.selectionTotalKnown ? state.selected.toLocaleString() : "...";
    $("#exactSelectedCount").textContent = state.exactSelectionKnown ? state.exactSelected.toLocaleString() : "...";
    $("#selectedSongCount").textContent = state.exactSelectedSongsKnown ? state.exactSelectedSongs.toLocaleString() : "...";
    $("#exactSelectedLabel").textContent = state.exactSelectionKnown && state.exactSelected === 1 ? "chart" : "charts";
    $("#selectedSongLabel").textContent = state.exactSelectedSongsKnown && state.exactSelectedSongs === 1 ? "song" : "songs";
    $("#expansionSelectedCount").textContent = state.expansionSelectionKnown ? state.expansionSelected.toLocaleString() : "...";
    const hasConfirmedSelection = exactConfirmed > 0 || expansionConfirmed > 0;
    $("#continueDownloadButton").disabled = !hasConfirmedSelection || state.selectionBusy || state.selectionStale;
    $("#continueDownloadButton").textContent = "Continue";
    $("#continueDownloadButton").setAttribute(
      "aria-label",
      state.selectionTotalKnown && state.selected
        ? `Continue with ${state.selected.toLocaleString()} selected ${state.selected === 1 ? "chart" : "charts"}`
        : hasConfirmedSelection ? "Continue with the currently confirmed chart selections" : "Continue with selected charts"
    );
    const modeLabels = {
      best_matches: "Last action: selected recommended charts.",
      all_safe_charts: "Last action: selected same-recording charts.",
      maximum_playable: "Last action: included alternate recordings.",
      custom: "Custom choices are saved.",
      none: "Selections are saved as you make them.",
    };
    $("#selectionMode").textContent = state.selectionStale
      ? "Last confirmed totals shown. Refresh the selection summary."
      : state.selectionTotalKnown ? modeLabels[state.selectionMode] || modeLabels.none : "Updating the combined selection total...";
    syncSelectionBand();
  }

  function syncSelectionBand() {
    const rail = $("#selectionRail");
    const stage = $("#stageConfirm");
    if (!rail || !stage) return;
    const compact = window.matchMedia("(max-width: 1000px)").matches;
    if (!compact || stage.hidden) {
      rail.classList.remove("is-in-flow");
      stage.style.removeProperty("padding-bottom");
      document.documentElement.style.removeProperty("scroll-padding-bottom");
      return;
    }
    rail.classList.remove("is-in-flow");
    window.requestAnimationFrame(() => {
      const visualViewport = window.visualViewport;
      const viewportHeight = visualViewport?.height || window.innerHeight;
      const viewportBottom = (visualViewport?.offsetTop || 0) + viewportHeight;
      const bandHeight = rail.getBoundingClientRect().height;
      const bandTop = viewportBottom - bandHeight;
      const focusedAction = document.activeElement?.matches?.('.candidate-row [data-selection]') ? document.activeElement : null;
      const firstAction = focusedAction || $('.candidate-row [data-selection]:not(:disabled)');
      const actionBox = firstAction?.getBoundingClientRect();
      const keyboardOpen = visualViewport && viewportHeight < window.innerHeight * .72;
      const actionCollision = Boolean(actionBox && actionBox.top < viewportBottom && actionBox.bottom + 7 > bandTop);
      const unsafe = viewportHeight < 600 || keyboardOpen || bandHeight > viewportHeight * .2 || actionCollision;
      rail.classList.toggle("is-in-flow", unsafe);
      const reservedSpace = `${Math.ceil(bandHeight) + 16}px`;
      stage.style.paddingBottom = unsafe ? "0" : reservedSpace;
      document.documentElement.style.scrollPaddingBottom = unsafe ? "0" : reservedSpace;
    });
  }

  function syncSelectionOptions() {
    const details = $(".selection-options");
    if (!details) return;
    const viewport = window.matchMedia("(max-width: 1000px)").matches ? "mobile" : "desktop";
    if (details.dataset.viewport === viewport) return;
    details.dataset.viewport = viewport;
    details.open = viewport === "desktop";
  }

  function disclosureOpenAttribute(key, containsSelection) {
    if (state.closedDisclosures.has(key)) return "";
    return state.openDisclosures.has(key) || containsSelection ? " open" : "";
  }

  function reviewCard(track, index) {
    const visibleTiers = new Set(["RECOMMENDED_EXACT", "SAME_RECORDING_CHART_VARIANT", "ALTERNATE_RECORDING", "POSSIBLE_MATCH"]);
    const candidates = (track.candidates || []).filter(candidate => visibleTiers.has(candidate.result_tier));
    const best = candidates.find(candidate => candidate.best) || candidates[0];
    const primary = best;
    if (!primary) return "";
    const groups = [
      ["SAME_RECORDING_CHART_VARIANT", "Other charts for this recording"],
      ["ALTERNATE_RECORDING", "Alternate versions"],
      ["POSSIBLE_MATCH", "Possible matches - review required"],
    ];
    const headingId = `song-group-title-${index + 1}`;
    return `<article class="review-card song-group" aria-labelledby="${headingId}">
      <header class="song-group__header">
        <span class="song-group__number" aria-hidden="true">${String(index + 1).padStart(2, "0")}</span>
        <div class="song-group__identity"><span>${escapeHtml(track.artist)}</span><h3 id="${headingId}">${escapeHtml(track.title)}</h3><p>${escapeHtml(track.album || "Album not provided")}</p></div>
        <span class="song-group__count">${candidates.length.toLocaleString()} ${candidates.length === 1 ? "chart" : "charts"}</span>
      </header>
      ${candidateRow(track, primary)}
      ${groups.map(([tier, label]) => {
        const values = candidates.filter(candidate => candidate !== primary && candidate.result_tier === tier);
        const key = `${track.track_key}:${tier}`;
        const open = disclosureOpenAttribute(key, values.some(candidate => candidate.selected));
        return values.length ? `<details class="alternatives" data-disclosure-key="${escapeHtml(key)}"${open}><summary>${label} (${values.length})</summary>${values.map(candidate => candidateRow(track, candidate)).join("")}</details>` : "";
      }).join("")}
    </article>`;
  }

  function candidateRow(track, candidate) {
    const tier = tierPresentation(candidate);
    const review = candidate.review || {};
    const focusKey = `${track.track_key}:${candidate.candidate_id}`;
    const instruments = (candidate.instruments || []).map(instrument => String(instrument).replace(/_/g, " "));
    const coverage = instruments.length ? instruments.join(", ") : "Instrument coverage not provided";
    const rawDifficulty = candidate.diff_guitar;
    const convertedDifficulty = Number(rawDifficulty);
    const guitarDifficulty = rawDifficulty !== null && rawDifficulty !== undefined && rawDifficulty !== "" && Number.isFinite(convertedDifficulty) && convertedDifficulty >= 0
      ? `Guitar difficulty ${rawDifficulty}`
      : "Guitar difficulty not provided";
    const concerns = (review.concerns || []).filter(
      concern => !duplicatesTierReason(tier.reason, concern)
    );
    const concernKey = `${focusKey}:concerns`;
    const concernOpen = disclosureOpenAttribute(concernKey, false);
    return `<div class="candidate-row ${candidate.selected ? "is-selected" : ""}">
      <div class="candidate-cell candidate-identity">
        <span class="match-badge ${candidate.result_tier === "POSSIBLE_MATCH" ? "match-badge--review" : ""}">${escapeHtml(tier.badge)}</span>
        <h4>${escapeHtml(candidate.title || "Untitled chart")}</h4>
        <p>${escapeHtml(candidate.artist || "Artist not provided")}</p>
      </div>
      <div class="candidate-cell candidate-classification ${tier.uncertain ? "is-uncertain" : ""}">
        <span class="candidate-label">Version</span><strong>${escapeHtml(tier.label)}</strong>
        ${tier.reason ? `<small>${escapeHtml(tier.reason)}</small>` : ""}
        ${concerns.length ? `<details class="concern-details" data-disclosure-key="${escapeHtml(concernKey)}"${concernOpen}><summary>${concerns.length} chart ${concerns.length === 1 ? "concern" : "concerns"}</summary><div>${concerns.map(item => `<p>${escapeHtml(item)}</p>`).join("")}</div></details>` : ""}
      </div>
      <div class="candidate-cell candidate-source"><span class="candidate-label">Chart source</span><strong>${escapeHtml(candidate.charter || "Charter not listed")}</strong><small>${escapeHtml(candidate.source || "Source not listed")}</small></div>
      <div class="candidate-cell candidate-evidence"><span class="candidate-label">Playable parts</span><strong>${escapeHtml(coverage)}</strong><small>${escapeHtml(guitarDifficulty)}</small></div>
      <div class="review-card__action">${candidateAction(track, candidate, focusKey)}</div>
    </div>`;
  }

  function candidateSummary(candidate, concernKey = "") {
    const tier = tierPresentation(candidate);
    const review = candidate.review || {};
    const instruments = (candidate.instruments || []).map(instrument => String(instrument).replace(/_/g, " "));
    const coverage = instruments.length ? instruments.join(", ") : "Instrument coverage not provided";
    const convertedDifficulty = Number(candidate.diff_guitar);
    const difficulty = candidate.diff_guitar !== null && candidate.diff_guitar !== undefined && candidate.diff_guitar !== "" && Number.isFinite(convertedDifficulty) && convertedDifficulty >= 0
      ? `Guitar difficulty ${candidate.diff_guitar}`
      : "Guitar difficulty not provided";
    const version = candidate.pack_name || candidate.source || "Community chart";
    const concerns = (review.concerns || []).filter(concern => !duplicatesTierReason(tier.reason, concern));
    const concernOpen = concernKey ? disclosureOpenAttribute(concernKey, false) : "";
    return `<div class="review-card__chart candidate-summary">
      <span class="match-badge ${candidate.result_tier === "POSSIBLE_MATCH" ? "match-badge--review" : ""}">${escapeHtml(tier.badge)}</span>
      <h4>${escapeHtml(candidate.artist)} - ${escapeHtml(candidate.title)}</h4>
      <p>${escapeHtml(version)}${candidate.charter ? ` · ${escapeHtml(candidate.charter)}` : ""}</p>
      <p>${escapeHtml(coverage)} · ${escapeHtml(difficulty)}</p>
      ${tier.reason ? `<p>${escapeHtml(tier.reason)}</p>` : ""}
      ${concerns.length ? `<details class="concern-details"${concernKey ? ` data-disclosure-key="${escapeHtml(concernKey)}"` : ""}${concernOpen}><summary>${concerns.length} chart ${concerns.length === 1 ? "concern" : "concerns"}</summary><div>${concerns.map(item => `<p>${escapeHtml(item)}</p>`).join("")}</div></details>` : ""}
    </div>`;
  }

  function tierPresentation(candidate) {
    const reason = humanizedTierReason(candidate);
    const live = /\blive\b/i.test(`${candidate.title || ""} ${(candidate.result_tier_reasons || []).join(" ")}`);
    const labels = {
      RECOMMENDED_EXACT: ["Recommended", "Recommended match"],
      SAME_RECORDING_CHART_VARIANT: ["Same recording", "Same recording · different chart"],
      ALTERNATE_RECORDING: [live ? "Live alternate" : "Alternate recording", live ? "Live alternate" : "Alternate recording"],
      POSSIBLE_MATCH: ["Possible match", "Possible match · review required"],
    };
    const [badge, label] = labels[candidate.result_tier] || [candidate.review?.label || "Chart", "Chart candidate"];
    return {badge, label, reason, uncertain: ["ALTERNATE_RECORDING", "POSSIBLE_MATCH"].includes(candidate.result_tier)};
  }

  function humanizedTierReason(candidate) {
    if (!candidate || candidate.result_tier === "RECOMMENDED_EXACT") return "";
    const raw = String((candidate.result_tier_reasons || [])[0] || "").toLowerCase();
    if (raw.includes("live") || /\blive\b/i.test(candidate.title || "")) {
      return "Different live recording.";
    } else if (raw.includes("duration")) {
      return "Recording duration differs.";
    } else if (raw.includes("version") || raw.includes("marker")) {
      return "Meaningful version markers differ.";
    } else if (raw.includes("identity") || raw.includes("review")) {
      return "Song identity requires explicit review.";
    } else if (candidate.result_tier === "SAME_RECORDING_CHART_VARIANT") {
      return "Another chart artifact for this recording.";
    } else if (candidate.result_tier === "ALTERNATE_RECORDING") {
      return "The recording differs from your imported song.";
    } else if (candidate.result_tier === "POSSIBLE_MATCH") {
      return "Song identity is uncertain.";
    }
    return "";
  }

  function duplicatesTierReason(reason, concern) {
    const value = String(concern || "").toLowerCase();
    if (reason === "Different live recording.") return value.includes("live recording") || value.includes("live version");
    if (reason === "Recording duration differs.") return ["duration", "recording length", "length differs"].some(term => value.includes(term));
    if (reason === "Meaningful version markers differ.") return ["version", "live", "remix", "cover"].some(term => value.includes(term));
    if (reason === "Song identity requires explicit review.") return value.includes("identity");
    if (reason === "Another chart artifact for this recording.") return value.includes("chart artifact") || value.includes("chart variant");
    return false;
  }

  function candidateAction(track, candidate, focusKey = `${track.track_key}:${candidate.candidate_id}`) {
    const title = candidate.title || track.title || "chart";
    const charter = candidate.charter || "unknown charter";
    const disclosureKey = `${track.track_key}:${candidate.result_tier}`;
    if (candidate.selected) return `<button class="button" type="button" data-selection="clear" data-track="${escapeHtml(track.track_key)}" data-candidate="${candidate.candidate_id}" data-focus-key="${escapeHtml(focusKey)}" data-disclosure-parent="${escapeHtml(disclosureKey)}" aria-label="Selected ${escapeHtml(title)} chart by ${escapeHtml(charter)}. Clear this choice."><span class="selection-check" aria-hidden="true">✓</span><span>Selected</span></button>`;
    if (!(candidate.selectable ?? candidate.review?.selectable)) return `<button class="button button--secondary" type="button" disabled>Unavailable</button>`;
    const possible = candidate.result_tier === "POSSIBLE_MATCH";
    const label = possible ? "Review and select" : "Select";
    return `<button class="button button--secondary" type="button" data-selection="select" data-track="${escapeHtml(track.track_key)}" data-candidate="${candidate.candidate_id}" data-possible="${possible}" data-focus-key="${escapeHtml(focusKey)}" data-disclosure-parent="${escapeHtml(disclosureKey)}" aria-label="${label} ${escapeHtml(title)} chart by ${escapeHtml(charter)} for ${escapeHtml(track.title)}">${label}</button>`;
  }

  function bindReviewActions() {
    $$('[data-selection]').forEach(button => button.addEventListener("click", () => updateSelection(button)));
    bindDisclosureState($("#reviewCards"));
  }

  function bindDisclosureState(root) {
    if (!root) return;
    $$('details[data-disclosure-key]', root).forEach(details => details.addEventListener("toggle", () => {
      const key = details.dataset.disclosureKey;
      if (details.open) {
        state.openDisclosures.add(key);
        state.closedDisclosures.delete(key);
      } else {
        state.closedDisclosures.add(key);
        state.openDisclosures.delete(key);
      }
    }));
  }

  async function updateSelection(button) {
    if (state.selectionBusy) return;
    const possible = button.dataset.possible === "true";
    if (possible && !window.confirm("This possible match has uncertain song identity. Select it anyway?")) return;
    state.selectionBusy = true;
    syncSelectionControls();
    refreshSelectionUi();
    state.reviewRequest += 1;
    clearSelectionFeedback();
    setButtonBusy(button, true, "Saving...");
    const focusKey = button.dataset.focusKey;
    const action = button.dataset.selection === "clear" ? "Chart choice cleared." : "Chart selected.";
    try {
      await api("/api/selection", {method: "POST", body: JSON.stringify({track_key: button.dataset.track, candidate_id: Number(button.dataset.candidate), action: button.dataset.selection, confirm_possible: possible})});
      state.selectionMode = "custom";
      if (button.dataset.selection === "select" && button.dataset.disclosureParent) {
        state.closedDisclosures.delete(button.dataset.disclosureParent);
        state.openDisclosures.add(button.dataset.disclosureParent);
      }
      const refreshed = await loadReview({focusKey, announcement: action, reconciliation: true});
      if (!refreshed) {
        setButtonBusy(button, false);
        markSelectionStale(action);
      }
    } catch (error) {
      const reconciled = await loadReview({focusKey, reconciliation: true});
      if (!reconciled) markSelectionStale(`${error.message || "This chart choice could not be saved."} The chart save result is not yet confirmed.`);
      else showSelectionError(error.message || "This chart choice could not be saved.");
    } finally {
      state.selectionBusy = false;
      setButtonBusy(button, false);
      syncSelectionControls();
      refreshSelectionUi();
    }
  }

  async function bulkSelect(scope) {
    if (state.selectionBusy) return;
    const button = scope === "best_matches" ? $("#selectBestButton") : scope === "all_safe_charts" ? $("#selectAllButton") : scope === "maximum_playable" ? $("#selectMaximumButton") : $("#clearSelectionButton");
    state.selectionBusy = true;
    syncSelectionControls();
    refreshSelectionUi();
    state.reviewRequest += 1;
    clearSelectionFeedback();
    setButtonBusy(button, true, "Saving choices...");
    try {
      const payload = scope === "clear" ? {action: "clear"} : {action: "select", preset: scope};
      await api("/api/selection/bulk", {method: "POST", body: JSON.stringify(payload)});
      state.selectionMode = scope === "clear" ? "none" : scope;
      state.reviewOffset = 0;
      const labels = {best_matches: "Recommended charts selected.", all_safe_charts: "Same-recording charts selected.", maximum_playable: "Alternate recordings included.", clear: "Choices from your music cleared."};
      const refreshed = await loadReview({announcement: labels[scope], reconciliation: true});
      if (!refreshed) markSelectionStale(labels[scope]);
    } catch (error) {
      const reconciled = await loadReview({reconciliation: true});
      if (!reconciled) markSelectionStale(`${error.message || "These chart choices could not be saved."} The preset save result is not yet confirmed.`);
      else showSelectionError(error.message || "These chart choices could not be saved.");
    } finally {
      state.selectionBusy = false;
      setButtonBusy(button, false);
      syncSelectionControls();
      refreshSelectionUi();
    }
  }

  async function retrySelectionSummary() {
    const button = $("#retrySelectionButton");
    if (!button || state.selectionBusy) return;
    state.selectionBusy = true;
    refreshSelectionUi();
    clearSelectionFeedback();
    setButtonBusy(button, true, "Refreshing...");
    try {
      const [reviewed, expanded] = await Promise.all([
        loadReview({reconciliation: true, preserveStale: true}),
        loadExpansion(false, {reconciliation: true}),
      ]);
      if (reviewed && expanded) {
        clearSelectionStale();
        clearSelectionFeedback();
        refreshSelectionUi();
        const message = state.selectionTotalKnown
          ? `Selection summary refreshed. ${state.selected.toLocaleString()} ${state.selected === 1 ? "chart" : "charts"} selected.`
          : "Selection summary refreshed. Updating the combined selection total.";
        $("#selectionMessage").textContent = message;
        announce(message);
      } else markSelectionStale("The selection summary is still unavailable.");
    } finally {
      state.selectionBusy = false;
      setButtonBusy(button, false);
      syncSelectionControls();
      refreshSelectionUi();
    }
  }

  async function loadExpansion(append = false, options = {}) {
    const panel = $("#expansionPanel");
    if (!panel) {
      state.expansionSelected = 0;
      state.expansionSelectionKnown = true;
      refreshSelectionUi();
      return true;
    }
    const requestId = ++state.expansionRequest;
    const choices = $("#expansionChoices");
    const status = $("#expansionStatus");
    const start = $("#startExpansionButton");
    try {
      if (!append) state.expansionOffset = 0;
      const [artistData, resultData] = await Promise.all([
        append ? Promise.resolve(null) : api("/api/expansion/artists"),
        api(`/api/expansion/results?limit=25&offset=${state.expansionOffset}`),
      ]);
      if (requestId !== state.expansionRequest) return false;
      const artists = artistData?.artists || [];
      if (artistData && !artistData.available) {
        choices.innerHTML = `<p class="empty-state">${escapeHtml(artistData.reason || "Artist expansion is not available yet.")}</p>`;
        start.disabled = true;
      } else if (artistData) {
        choices.innerHTML = artists.map(artist => `<label>
          <input type="checkbox" value="${escapeHtml(artist.artist_norm)}" ${artist.selected ? "checked" : ""}>
          <span><strong>${escapeHtml(artist.artist)}</strong><small>${Number(artist.eligible_songs || 0).toLocaleString()} eligible songs · up to ${Number(artist.cap || 0).toLocaleString()} recommended</small></span>
        </label>`).join("") || `<p class="empty-state">No imported artists have additional eligible catalog songs.</p>`;
        start.disabled = artists.length === 0;
        choices.onchange = () => {
          const count = $$("input:checked", choices).length;
          start.disabled = count === 0 || count > Number(artistData.max_artists || 25);
          status.textContent = count > Number(artistData.max_artists || 25)
            ? `Choose no more than ${Number(artistData.max_artists || 25)} artists.`
            : `${count.toLocaleString()} ${count === 1 ? "artist" : "artists"} selected.`;
        };
      }
      const pageCards = resultData.songs || [];
      state.expansionCards = append
        ? [...state.expansionCards, ...pageCards].filter((item, index, values) => values.findIndex(other => other.song_key === item.song_key) === index)
        : pageCards;
      const cards = state.expansionCards;
      state.expansionOffset = cards.length;
      const expansionSelected = availableNumber(resultData.summary, ["selected"]);
      if (expansionSelected !== null) {
        state.expansionSelected = expansionSelected;
        state.expansionSelectionKnown = true;
      }
      refreshSelectionUi();
      $("#expansionResults").hidden = cards.length === 0;
      $("#expansionResultCount").textContent = Number(resultData.total || 0).toLocaleString();
      $("#expansionCards").innerHTML = cards.map(expansionCard).join("");
      const more = $("#loadMoreExpansion");
      more.hidden = !resultData.has_more;
      more.onclick = () => loadExpansion(true);
      bindExpansionActions();
      const job = artistData?.job;
      if (job && ["queued", "running"].includes(job.status)) {
        status.textContent = job.message || "Preparing expansion recommendations...";
        start.disabled = true;
        attachExpansionJob(job.id);
      } else if (job?.status === "complete" && cards.length) {
        status.textContent = `${Number(resultData.total || 0).toLocaleString()} additional songs are ready to review.`;
      }
      return true;
    } catch (error) {
      if (requestId !== state.expansionRequest) return false;
      if (options.reconciliation) state.expansionSelectionKnown = false;
      choices.innerHTML = `<p class="empty-state">${escapeHtml(error.message || "Expansion choices could not be loaded.")}</p>`;
      start.disabled = true;
      refreshSelectionUi();
      return false;
    }
  }

  function expansionCard(song) {
    const candidates = song.candidates || [];
    const primary = candidates.find(candidate => candidate.selected) || candidates[0];
    if (!primary) return "";
    const alternatives = candidates.filter(candidate => candidate !== primary);
    const alternativesKey = `expansion:${song.song_key}:alternatives`;
    const alternativesOpen = disclosureOpenAttribute(alternativesKey, alternatives.some(candidate => candidate.selected));
    return `<article class="review-card review-card--expansion">
      <div class="review-card__song"><span class="eyebrow">ARTIST EXPANSION</span><h3>${escapeHtml(song.artist)} - ${escapeHtml(song.title)}</h3><p>Not in your imported library</p></div>
      ${candidateSummary(primary, `expansion:${song.song_key}:${primary.candidate_id}:concerns`)}
      <div class="review-card__action">${expansionCandidateAction(song, primary)}</div>
      ${alternatives.length ? `<details class="alternatives" data-disclosure-key="${escapeHtml(alternativesKey)}"${alternativesOpen}><summary>Other chart versions (${alternatives.length})</summary>${alternatives.map(candidate => `<div class="alternative">${candidateSummary(candidate, `expansion:${song.song_key}:${candidate.candidate_id}:concerns`)}<div class="review-card__action">${expansionCandidateAction(song, candidate)}</div></div>`).join("")}</details>` : ""}
    </article>`;
  }

  function expansionCandidateAction(song, candidate) {
    const focusKey = `${song.song_key}:${candidate.candidate_id}`;
    if (candidate.selected) return `<button class="button button--secondary" type="button" data-expansion-selection="clear" data-artist="${escapeHtml(candidate.artist_norm)}" data-candidate="${candidate.candidate_id}" data-expansion-focus="${escapeHtml(focusKey)}">Clear choice</button>`;
    if (!candidate.selectable) return `<button class="button button--secondary" type="button" disabled>Unavailable</button>`;
    const possible = candidate.result_tier === "POSSIBLE_MATCH";
    return `<button class="button button--secondary" type="button" data-expansion-selection="select" data-artist="${escapeHtml(candidate.artist_norm)}" data-candidate="${candidate.candidate_id}" data-possible="${possible}" data-expansion-focus="${escapeHtml(focusKey)}">${possible ? "Review and select" : "Select"}</button>`;
  }

  function bindExpansionActions() {
    $$('[data-expansion-selection]').forEach(button => button.addEventListener("click", () => updateExpansionSelection(button)));
    bindDisclosureState($("#expansionCards"));
  }

  async function updateExpansionSelection(button) {
    if (state.selectionBusy) return;
    const possible = button.dataset.possible === "true";
    if (possible && !window.confirm("This possible expansion match needs explicit confirmation. Select it?")) return;
    const focusKey = button.dataset.expansionFocus;
    const action = button.dataset.expansionSelection === "clear" ? "Additional chart choice cleared." : "Additional chart selected.";
    state.selectionBusy = true;
    syncSelectionControls();
    refreshSelectionUi();
    clearSelectionFeedback();
    setButtonBusy(button, true, "Saving...");
    try {
      await api("/api/selection", {method: "POST", body: JSON.stringify({
        kind: "expansion",
        artist_norm: button.dataset.artist,
        candidate_id: Number(button.dataset.candidate),
        action: button.dataset.expansionSelection,
        confirm_possible: possible,
      })});
      state.selectionMode = "custom";
      state.expansionSelectionKnown = false;
      refreshSelectionUi();
      const refreshed = await loadExpansion(false, {reconciliation: true});
      if (refreshed) {
        const replacement = $$('[data-expansion-focus]').find(node => node.dataset.expansionFocus === focusKey);
        replacement?.focus({preventScroll: true});
        const message = state.selectionTotalKnown
          ? `${action} ${state.selected.toLocaleString()} ${state.selected === 1 ? "chart" : "charts"} selected.`
          : `${action} Updating the combined selection total.`;
        $("#selectionMessage").textContent = message;
        announce(message);
      } else markSelectionStale(action);
    } catch (error) {
      const [reviewed, expanded] = await Promise.all([
        loadReview({reconciliation: true, preserveStale: true}),
        loadExpansion(false, {reconciliation: true}),
      ]);
      if (!reviewed || !expanded) markSelectionStale(`${error.message || "This additional chart choice could not be saved."} The save result is not yet confirmed.`);
      else showSelectionError(error.message || "This additional chart choice could not be saved.");
    } finally {
      state.selectionBusy = false;
      setButtonBusy(button, false);
      syncSelectionControls();
      refreshSelectionUi();
    }
  }

  async function startExpansion() {
    const button = $("#startExpansionButton");
    const selected = $$("#expansionChoices input:checked").map(input => input.value);
    clearError();
    setButtonBusy(button, true, "Starting...");
    try {
      const result = await api("/api/expansion", {method: "POST", body: JSON.stringify({artists: selected})});
      attachExpansionJob(result.job.id);
    } catch (error) {
      showError(error.message);
      setButtonBusy(button, false);
    }
  }

  function attachExpansionJob(jobId) {
    const expectedJobId = Number(jobId);
    if (state.expansionPolling && state.expansionJobId === expectedJobId) return;
    if (state.expansionPollTimer) window.clearTimeout(state.expansionPollTimer);
    state.expansionJobId = expectedJobId;
    state.expansionPollTimer = null;
    state.expansionPolling = true;
    void pollExpansionJob(expectedJobId);
  }

  async function pollExpansionJob(expectedJobId = state.expansionJobId) {
    if (!state.expansionPolling || !expectedJobId || expectedJobId !== state.expansionJobId) return;
    const status = $("#expansionStatus");
    try {
      const {job} = await api(`/api/jobs/${expectedJobId}`);
      if (!state.expansionPolling || expectedJobId !== state.expansionJobId) return;
      const done = Number(job.progress?.completed_artists || 0);
      const total = Number(job.progress?.total_artists || 0);
      status.textContent = job.status === "queued"
        ? "Expansion is queued and will start shortly."
        : job.message || (total ? `${done} of ${total} artists prepared.` : "Preparing artist recommendations...");
      if (isTerminal(job)) {
        window.clearTimeout(state.expansionPollTimer);
        state.expansionPollTimer = null;
        state.expansionPolling = false;
        state.expansionJobId = null;
        setButtonBusy($("#startExpansionButton"), false);
        if (job.status !== "complete") showError(job.error || "Artist expansion did not finish.");
        await loadExpansion();
        return;
      }
    } catch (error) {
      if (expectedJobId !== state.expansionJobId) return;
      window.clearTimeout(state.expansionPollTimer);
      state.expansionPollTimer = null;
      state.expansionPolling = false;
      showError(error.message);
      return;
    }
    if (state.expansionPolling && expectedJobId === state.expansionJobId) {
      state.expansionPollTimer = window.setTimeout(() => { void pollExpansionJob(expectedJobId); }, 1200);
    }
  }

  function openDownload() {
    if (state.selectionBusy || state.selectionStale) {
      showSelectionError("Refresh the selection summary before continuing.");
      return;
    }
    if (state.selected < 1) {
      showSelectionError("Select at least one chart before continuing.");
      return;
    }
    $("#downloadSelectionCount").textContent = state.selected.toLocaleString();
    showStage("download", true);
  }

  async function prepareDownload() {
    const button = $("#prepareDownloadButton");
    const status = $("#downloadStatus");
    clearError();
    setButtonBusy(button, true, "Validating and packaging...");
    status.textContent = "Riff is downloading and validating each selected chart. Keep this page open while the ZIP is prepared.";
    try {
      const result = await api("/api/download", {method: "POST", body: "{}"});
      renderDone({...result.download, url: `/api/download/${result.download.token}`}, result.summary);
    } catch (error) {
      status.textContent = "";
      showError(error.message);
    } finally {
      setButtonBusy(button, false);
    }
  }

  function renderDone(download, summary) {
    const link = $("#downloadLibraryLink");
    link.href = download.url || `/api/download/${download.token}`;
    if (summary) {
      const ready = Number(summary.ready || summary.published || state.selected);
      $("#doneSummary").textContent = `${ready.toLocaleString()} validated ${ready === 1 ? "chart is" : "charts are"} packaged. Download the ZIP, then add its song folders to Clone Hero.`;
    }
    const expires = Number(download.expires_at || 0);
    $("#downloadExpiry").textContent = expires ? `This single-use link expires at ${new Date(expires * 1000).toLocaleTimeString([], {hour: "numeric", minute: "2-digit"})}.` : "This is a short-lived, single-use download link.";
    showStage("done", true);
    announce("Your Riff library is ready to download.");
  }

  async function prepareAgain() {
    state.journey = await api("/api/journey");
    state.exactSelected = Number(state.journey.review?.selected || 0);
    if ($("#expansionPanel")) {
      const expansion = await api("/api/expansion/results").catch(() => ({summary: {selected: 0}}));
      state.expansionSelected = Number(expansion.summary?.selected || 0);
    }
    refreshSelectionUi();
    openDownload();
  }

  function debounce(callback, delay = 220) {
    let timer;
    return (...args) => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => callback(...args), delay);
    };
  }

  function bindAuthenticated() {
    syncSelectionOptions();
    window.addEventListener("resize", () => {
      syncSelectionOptions();
      syncSelectionBand();
    });
    window.visualViewport?.addEventListener("resize", syncSelectionBand);
    document.addEventListener("focusin", event => {
      if (event.target?.matches?.('.candidate-row [data-selection]')) syncSelectionBand();
    });
    $("#soundcloudForm")?.addEventListener("submit", importAuthenticatedSoundCloud);
    $("#selectAllPlaylistsButton")?.addEventListener("click", () => setAllSpotifyPlaylists(true));
    $("#clearPlaylistsButton")?.addEventListener("click", () => setAllSpotifyPlaylists(false));
    $("#refreshSpotifyButton")?.addEventListener("click", () => refreshSpotify(false));
    $("#findChartsButton")?.addEventListener("click", startDiscovery);
    $("#cancelJobButton")?.addEventListener("click", cancelDiscovery);
    $("#activityDetails")?.addEventListener("toggle", event => { if (event.currentTarget.open) loadActivity(); });
    $("#activitySearch")?.addEventListener("input", debounce(() => { state.activityOffset = 0; loadActivity(); }));
    $("#activityPrev")?.addEventListener("click", () => { state.activityOffset = Math.max(0, state.activityOffset - 50); loadActivity(); });
    $("#activityNext")?.addEventListener("click", () => { state.activityOffset += 50; loadActivity(); });
    $("#selectBestButton")?.addEventListener("click", () => bulkSelect("best_matches"));
    $("#selectAllButton")?.addEventListener("click", () => bulkSelect("all_safe_charts"));
    $("#selectMaximumButton")?.addEventListener("click", () => bulkSelect("maximum_playable"));
    $("#clearSelectionButton")?.addEventListener("click", () => bulkSelect("clear"));
    $("#retrySelectionButton")?.addEventListener("click", retrySelectionSummary);
    $("#reviewSearch")?.addEventListener("input", debounce(event => { state.reviewSearch = event.target.value.trim(); state.reviewOffset = 0; loadReview(); }));
    $$('[data-review-filter]').forEach(button => button.addEventListener("click", () => {
      state.reviewFilter = button.dataset.reviewFilter;
      state.reviewOffset = 0;
      $$('[data-review-filter]').forEach(item => item.setAttribute("aria-pressed", String(item === button)));
      loadReview();
    }));
    $("#reviewPrev")?.addEventListener("click", () => { state.reviewOffset = Math.max(0, state.reviewOffset - 25); loadReview(); });
    $("#reviewNext")?.addEventListener("click", () => { state.reviewOffset += 25; loadReview(); });
    $("#startExpansionButton")?.addEventListener("click", startExpansion);
    $("#continueDownloadButton")?.addEventListener("click", openDownload);
    $("#backToChartsButton")?.addEventListener("click", () => openConfirm(true));
    $("#searchAgainButton")?.addEventListener("click", () => openAddMusic(true));
    $("#prepareDownloadButton")?.addEventListener("click", prepareDownload);
    $("#downloadLibraryLink")?.addEventListener("click", () => announce("Your browser download has started. Riff cannot confirm when your browser finishes saving it."));
    $("#prepareAgainButton")?.addEventListener("click", prepareAgain);
    restore();
  }

  $("#logoutButton")?.addEventListener("click", logout);
  if (authenticated && app) bindAuthenticated(); else bindGuest();
})();
