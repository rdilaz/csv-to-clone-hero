(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const authenticated = document.body.dataset.authenticated === "true";
  const app = $("#riffApp");
  const state = {
    journey: null,
    scope: null,
    jobId: null,
    pollTimer: null,
    activityOffset: 0,
    activityTotal: 0,
    reviewOffset: 0,
    reviewTotal: 0,
    reviewFilter: "recommended",
    reviewSearch: "",
    selected: 0,
    exactSelected: 0,
    expansionSelected: 0,
    expansionJobId: null,
    expansionPollTimer: null,
    expansionOffset: 0,
    expansionCards: [],
    selectionMode: "none",
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
      const error = new Error(data.error || `Request failed (${response.status})`);
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
    announce(message);
  }

  function clearError() {
    const node = $("#appError");
    if (node) node.hidden = true;
  }

  function setButtonBusy(button, busy, busyText) {
    if (!button) return;
    if (button._riffLabel === undefined) button._riffLabel = button.innerHTML;
    button.disabled = busy;
    if (!button.classList.contains("cohort-card")) {
      if (busy) button.textContent = busyText;
      else button.innerHTML = button._riffLabel;
    }
    button.setAttribute("aria-busy", String(busy));
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
    const currentStep = $$(".journey-steps li")[current];
    currentStep?.scrollIntoView({behavior: "smooth", block: "nearest", inline: "center"});
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
        attachExpansionJob(journey.job.id);
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
      await openAddMusic(false);
    } catch (error) {
      showStage("add");
      showError(error.message || "Riff could not restore your library.");
    }
  }

  async function openAddMusic(focus = true) {
    showStage("add", focus);
    try {
      const [journey, scope, soundcloud] = await Promise.all([
        api("/api/journey"),
        api("/api/spotify/scope"),
        api("/api/auth/soundcloud/status").catch(() => ({configured: false})),
      ]);
      state.journey = journey;
      state.scope = scope;
      renderSources(journey);
      renderScope(scope);
      await renderSoundCloudAuth(soundcloud);
    } catch (error) {
      showError(error.message || "Your music could not be loaded. Reload to try again.");
    }
  }

  function renderScope(payload) {
    const host = $("#spotifyChoices");
    if (!host) return;
    const saved = payload.workspace?.scope || {};
    const selectedIds = new Set(saved.playlist_ids || []);
    const playlistRows = (payload.playlists || []).map(item => {
      const count = item.track_count === null || item.track_count === undefined ? null : Number(item.track_count);
      const displayCount = Number.isFinite(count) ? count.toLocaleString() : "Count unavailable";
      const tallyCount = Number.isFinite(count) ? String(count) : "";
      return `<label><input class="spotify-playlist" type="checkbox" value="${escapeHtml(item.id)}" data-track-count="${tallyCount}" ${selectedIds.has(item.id) ? "checked" : ""}><span>${escapeHtml(item.name || "Untitled playlist")}</span><small>${displayCount}</small></label>`;
    });
    const rows = [
      `<label><input id="includeLiked" type="checkbox" ${saved.include_liked ? "checked" : ""}><span>Liked Songs</span><small>${Number(payload.liked_count || 0).toLocaleString()}</small></label>`,
      ...playlistRows,
    ];
    host.innerHTML = rows.join("") || "<p>No Spotify playlists were returned.</p>";
    host.setAttribute("aria-busy", "false");
    host.addEventListener("change", updateMusicTally);
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
      const likedLabel = journey.spotify_scope?.include_liked ? " + Liked Songs" : "";
      const playlistLabel = `${playlists.toLocaleString()} ${playlists === 1 ? "playlist" : "playlists"}`;
      const sourceItemLabel = `${spotify?.raw_items_lower_bound ? "at least " : ""}${raw.toLocaleString()} source items`;
      spotifySummary.textContent = unique || playlists
        ? `${playlistLabel}${likedLabel}; ${sourceItemLabel} became ${unique.toLocaleString()} unique ${unique === 1 ? "song" : "songs"} (${liked.toLocaleString()} liked, ${playlistOnly.toLocaleString()} playlist-only).`
        : "";
    }
    const count = Number(imported?.unique_tracks || imported?.tracks || 0);
    const playlistCount = Number(imported?.playlists || 0);
    const badge = $("#soundcloudCount");
    if (badge) {
      badge.textContent = count || playlistCount
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
      await api("/api/soundcloud/scope", {method: "POST", body: JSON.stringify({
        include_liked: Boolean($("#soundcloudLiked")?.checked),
        playlist_ids: $$(".soundcloud-playlist:checked").map(input => input.value),
      })});
      message.textContent = "SoundCloud selection saved.";
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

  function updateMusicTally() {
    const tally = $("#musicTally");
    if (!tally) return;
    const liked = $("#includeLiked")?.checked ? Number(state.scope?.liked_count || 0) : 0;
    let hasUnknownPlaylistCount = false;
    const playlistCount = $$(".spotify-playlist:checked").reduce((sum, input) => {
      if (!input.dataset.trackCount) {
        hasUnknownPlaylistCount = true;
        return sum;
      }
      return sum + Number(input.dataset.trackCount);
    }, 0);
    const imported = Number(state.journey?.combined?.tracks || 0);
    const estimated = liked + playlistCount;
    const strong = tally.querySelector("strong");
    const small = tally.querySelector("small");
    if (estimated || imported || hasUnknownPlaylistCount) {
      strong.textContent = hasUnknownPlaylistCount
        ? estimated ? `At least ${estimated.toLocaleString()} Spotify tracks chosen` : "Spotify playlists chosen"
        : `${(estimated || imported).toLocaleString()} ${estimated ? "Spotify tracks chosen" : "imported songs"}`;
      small.textContent = hasUnknownPlaylistCount
        ? "Some playlist counts are unavailable; the exact total will be known after import. Duplicates are merged."
        : imported && estimated ? `${imported.toLocaleString()} imported SoundCloud songs will also be included; duplicates are merged.` : "Duplicates are merged before matching.";
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

  function attachJob(jobId) {
    state.jobId = Number(jobId);
    showStage("find");
    if (state.pollTimer) window.clearInterval(state.pollTimer);
    pollJob();
    state.pollTimer = window.setInterval(pollJob, 1500);
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

  async function pollJob() {
    try {
      const {job} = await api(`/api/jobs/${state.jobId}`);
      renderProgress(job);
      if ($("#activityDetails")?.open) loadActivity();
      if (isTerminal(job)) {
        window.clearInterval(state.pollTimer);
        state.pollTimer = null;
        state.journey = await api("/api/journey");
        if (job.status === "failed") showError("The search stopped before it finished. Your completed work is saved; start again when ready.");
        else if (job.status !== "complete") showError("The search was interrupted. Saved results are available, but some songs may still need checking.");
        await openConfirm(true);
      }
    } catch (error) {
      window.clearInterval(state.pollTimer);
      state.pollTimer = null;
      showError(error.message);
    }
  }

  function renderProgress(job) {
    const {progress, total, done, percent, unit} = progressNumbers(job);
    const disc = $("#progressDisc");
    disc.style.setProperty("--progress", `${percent}%`);
    if (total) disc.setAttribute("aria-valuenow", String(percent)); else disc.removeAttribute("aria-valuenow");
    disc.setAttribute("aria-valuetext", total ? `${done} of ${total} ${unit} complete` : "Preparing music");
    $("#progressPercent").textContent = total ? `${percent}%` : "...";
    $("#progressBar").style.width = `${percent}%`;
    $("#progressCount").textContent = total ? `${done.toLocaleString()} of ${total.toLocaleString()} ${unit} complete` : "Preparing your music";
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
    $("#progressMessage").textContent = phases[job.status === "queued" ? "queued" : job.phase] || job.message || "Finding charts...";
    const current = progress.current_song;
    $("#currentSong").textContent = typeof current === "string" ? current : current?.artist && current?.title ? `${current.artist} - ${current.title}` : "";
  }

  async function loadActivity() {
    if (!state.jobId) return;
    const params = new URLSearchParams({limit: "50", offset: String(state.activityOffset)});
    const search = $("#activitySearch")?.value.trim();
    if (search) params.set("search", search);
    const data = await api(`/api/jobs/${state.jobId}/songs?${params}`);
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
    await Promise.all([loadReview(), loadExpansion()]);
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

  async function loadReview() {
    try {
      const data = await api(`/api/workspace?${reviewParams()}`);
      state.reviewTotal = Number(data.total || 0);
      state.exactSelected = Number(data.summary?.selection_count || 0);
      const legacyRecommended = Number(data.summary?.strong || 0) + Number(data.summary?.review || 0);
      const recommended = Number(data.summary?.recommended_songs ?? legacyRecommended);
      const additional = Number(data.summary?.additional_playable_charts || 0);
      const selectable = Number(data.summary?.selectable_charts ?? legacyRecommended);
      $("#chartsFoundCount").textContent = recommended.toLocaleString();
      $("#chartsFoundLabel").textContent = recommended === 1 ? "song has a recommended chart." : "songs have recommended charts.";
      $("#bestMatchCount").textContent = `${recommended.toLocaleString()} ${recommended === 1 ? "chart" : "charts"}`;
      $("#allMatchCount").textContent = `${Number(data.summary?.all_safe_charts ?? recommended).toLocaleString()} safe charts`;
      $("#maximumMatchCount").textContent = `${Number(data.summary?.maximum_playable_charts ?? selectable).toLocaleString()} playable charts`;
      if (state.exactSelected < 1 && state.expansionSelected < 1) state.selectionMode = "none";
      else if (state.selectionMode === "none") state.selectionMode = "custom";
      $("#selectBestButton").setAttribute("aria-pressed", String(state.selectionMode === "best_matches"));
      $("#selectAllButton").setAttribute("aria-pressed", String(state.selectionMode === "all_safe_charts"));
      $("#selectMaximumButton").setAttribute("aria-pressed", String(state.selectionMode === "maximum_playable"));
      const modeLabels = {best_matches: "Best Matches preset", all_safe_charts: "All Safe Charts preset", maximum_playable: "Maximum Playable preset", custom: "Custom choices", none: "No selection preset"};
      $("#selectionMode").textContent = modeLabels[state.selectionMode];
      refreshSelectionUi();
      $("#reviewSummary").textContent = `${recommended.toLocaleString()} songs have recommendations; ${additional.toLocaleString()} additional playable charts are available.`;
      $("#reviewCards").innerHTML = (data.tracks || []).map(reviewCard).join("") || `<div class="empty-state">No recommendations match this view.</div>`;
      bindReviewActions();
      const page = Math.floor(state.reviewOffset / 25) + 1;
      $("#reviewPage").textContent = `${page} / ${Math.max(1, Math.ceil(state.reviewTotal / 25))}`;
      $("#reviewPrev").disabled = state.reviewOffset === 0;
      $("#reviewNext").disabled = state.reviewOffset + 25 >= state.reviewTotal;
    } catch (error) {
      showError(error.message);
    }
  }

  function refreshSelectionUi() {
    state.selected = state.exactSelected + state.expansionSelected;
    $("#selectedCount").textContent = state.selected.toLocaleString();
    $("#continueDownloadButton").disabled = state.selected < 1;
    $("#continueDownloadButton").textContent = state.selected
      ? `Continue with ${state.selected.toLocaleString()} selected ${state.selected === 1 ? "chart" : "charts"}`
      : "Continue with selected charts";
  }

  function reviewCard(track) {
    const candidates = track.candidates || [];
    const best = candidates.find(candidate => candidate.best) || candidates[0];
    const primary = best;
    if (!primary) return "";
    const groups = [
      ["SAME_RECORDING_CHART_VARIANT", "Other charts for this recording"],
      ["ALTERNATE_RECORDING", "Alternate versions"],
      ["POSSIBLE_MATCH", "Possible matches - review required"],
    ];
    return `<article class="review-card">
      <div class="review-card__song"><span class="eyebrow">YOUR MUSIC</span><h3>${escapeHtml(track.artist)} - ${escapeHtml(track.title)}</h3><p>${escapeHtml(track.album || "")}</p></div>
      ${candidateSummary(primary, primary === best)}
      <div class="review-card__action">${candidateAction(track, primary)}</div>
      ${groups.map(([tier, label]) => {
        const values = candidates.filter(candidate => candidate !== primary && candidate.result_tier === tier);
        return values.length ? `<details class="alternatives"><summary>${label} (${values.length})</summary>${values.map(candidate => `<div class="alternative">${candidateSummary(candidate, false)}<div class="review-card__action">${candidateAction(track, candidate)}</div></div>`).join("")}</details>` : "";
      }).join("")}
    </article>`;
  }

  function candidateSummary(candidate, best) {
    const review = candidate.review || {};
    const version = candidate.pack_name || candidate.source || "Community chart";
    const tierLabels = {RECOMMENDED_EXACT: "Best match", SAME_RECORDING_CHART_VARIANT: "Chart variant", ALTERNATE_RECORDING: "Alternate recording", POSSIBLE_MATCH: "Possible match"};
    const coverage = (candidate.instruments || []).join(", ") || "Instrument coverage unknown";
    const tierReason = humanizedTierReason(candidate);
    const concerns = (review.concerns || []).filter(
      concern => !duplicatesTierReason(tierReason, concern)
    );
    return `<div class="review-card__chart">
      <span class="match-badge ${candidate.result_tier === "POSSIBLE_MATCH" ? "match-badge--review" : ""}">${candidate.selected ? "Selected chart" : escapeHtml(tierLabels[candidate.result_tier] || review.label || "Chart")}</span>
      <h4>${escapeHtml(candidate.artist)} - ${escapeHtml(candidate.title)}</h4>
      <p>${escapeHtml(version)}${candidate.charter ? ` · ${escapeHtml(candidate.charter)}` : ""}</p>
      <p>${escapeHtml(coverage)}${candidate.diff_guitar !== null && candidate.diff_guitar !== undefined ? ` · guitar difficulty ${escapeHtml(candidate.diff_guitar)}` : " · difficulty evidence missing"}</p>
      ${tierReason ? `<p>${escapeHtml(tierReason)}</p>` : ""}
      ${concerns.length ? `<details class="concern-details"><summary>${concerns.length} chart ${concerns.length === 1 ? "concern" : "concerns"}</summary><div>${concerns.map(item => `<p>${escapeHtml(item)}</p>`).join("")}</div></details>` : ""}
    </div>`;
  }

  function humanizedTierReason(candidate) {
    if (!candidate || candidate.result_tier === "RECOMMENDED_EXACT") return "";
    const raw = String((candidate.result_tier_reasons || [])[0] || "").toLowerCase();
    if (raw.includes("duration")) {
      return "Recording duration differs.";
    } else if (raw.includes("version") || raw.includes("marker")) {
      return "Meaningful version markers differ.";
    } else if (raw.includes("identity") || raw.includes("review")) {
      return "Song identity requires explicit review.";
    } else if (candidate.result_tier === "SAME_RECORDING_CHART_VARIANT") {
      return "Another chart artifact for this recording.";
    }
    return "";
  }

  function duplicatesTierReason(reason, concern) {
    const value = String(concern || "").toLowerCase();
    if (reason === "Recording duration differs.") return ["duration", "recording length", "length differs"].some(term => value.includes(term));
    if (reason === "Meaningful version markers differ.") return ["version", "live", "remix", "cover"].some(term => value.includes(term));
    if (reason === "Song identity requires explicit review.") return value.includes("identity");
    if (reason === "Another chart artifact for this recording.") return value.includes("chart artifact") || value.includes("chart variant");
    return false;
  }

  function candidateAction(track, candidate) {
    if (candidate.selected) return `<button class="button button--secondary" type="button" data-selection="clear" data-track="${escapeHtml(track.track_key)}" data-candidate="${candidate.candidate_id}" aria-label="Clear selected chart for ${escapeHtml(track.title)}">Clear choice</button>`;
    if (!(candidate.selectable ?? candidate.review?.selectable)) return `<button class="button button--secondary" type="button" disabled>Unavailable</button>`;
    const possible = candidate.result_tier === "POSSIBLE_MATCH";
    return `<button class="button button--secondary" type="button" data-selection="select" data-track="${escapeHtml(track.track_key)}" data-candidate="${candidate.candidate_id}" data-possible="${possible}">${possible ? "Review and select" : "Select"}</button>`;
  }

  function bindReviewActions() {
    $$('[data-selection]').forEach(button => button.addEventListener("click", () => updateSelection(button)));
  }

  async function updateSelection(button) {
    setButtonBusy(button, true, "Saving...");
    try {
      const possible = button.dataset.possible === "true";
      if (possible && !window.confirm("This possible match has uncertain song identity. Select it anyway?")) { setButtonBusy(button, false); return; }
      await api("/api/selection", {method: "POST", body: JSON.stringify({track_key: button.dataset.track, candidate_id: Number(button.dataset.candidate), action: button.dataset.selection, confirm_possible: possible})});
      state.selectionMode = "custom";
      announce(button.dataset.selection === "clear" ? "Chart choice cleared." : "Chart choice saved.");
      await loadReview();
    } catch (error) {
      showError(error.message);
      setButtonBusy(button, false);
    }
  }

  async function bulkSelect(scope) {
    const button = scope === "best_matches" ? $("#selectBestButton") : scope === "all_safe_charts" ? $("#selectAllButton") : scope === "maximum_playable" ? $("#selectMaximumButton") : $("#clearSelectionButton");
    setButtonBusy(button, true, "Saving choices...");
    try {
      const payload = scope === "clear" ? {action: "clear"} : {action: "select", preset: scope};
      await api("/api/selection/bulk", {method: "POST", body: JSON.stringify(payload)});
      state.selectionMode = scope === "clear" ? "none" : scope;
      $("#selectBestButton").setAttribute("aria-pressed", String(scope === "best_matches"));
      $("#selectAllButton").setAttribute("aria-pressed", String(scope === "all_safe_charts"));
      $("#selectMaximumButton").setAttribute("aria-pressed", String(scope === "maximum_playable"));
      announce(scope === "clear" ? "Chart choices cleared." : "Chart choices saved.");
      state.reviewOffset = 0;
      await loadReview();
    } catch (error) {
      showError(error.message);
    } finally {
      setButtonBusy(button, false);
    }
  }

  async function loadExpansion(append = false) {
    const panel = $("#expansionPanel");
    if (!panel) return;
    const choices = $("#expansionChoices");
    const status = $("#expansionStatus");
    const start = $("#startExpansionButton");
    try {
      if (!append) state.expansionOffset = 0;
      const [artistData, resultData] = await Promise.all([
        append ? Promise.resolve(null) : api("/api/expansion/artists"),
        api(`/api/expansion/results?limit=25&offset=${state.expansionOffset}`),
      ]);
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
      state.expansionSelected = Number(resultData.summary?.selected || 0);
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
        if (!state.expansionPollTimer) attachExpansionJob(job.id);
      } else if (job?.status === "complete" && cards.length) {
        status.textContent = `${Number(resultData.total || 0).toLocaleString()} additional songs are ready to review.`;
      }
    } catch (error) {
      choices.innerHTML = `<p class="empty-state">${escapeHtml(error.message || "Expansion choices could not be loaded.")}</p>`;
      start.disabled = true;
    }
  }

  function expansionCard(song) {
    const candidates = song.candidates || [];
    const primary = candidates.find(candidate => candidate.selected) || candidates[0];
    if (!primary) return "";
    const alternatives = candidates.filter(candidate => candidate !== primary);
    return `<article class="review-card review-card--expansion">
      <div class="review-card__song"><span class="eyebrow">ARTIST EXPANSION</span><h3>${escapeHtml(song.artist)} - ${escapeHtml(song.title)}</h3><p>Not in your imported library</p></div>
      ${candidateSummary(primary, Boolean(primary.best))}
      <div class="review-card__action">${expansionCandidateAction(song, primary)}</div>
      ${alternatives.length ? `<details class="alternatives"><summary>Other chart versions (${alternatives.length})</summary>${alternatives.map(candidate => `<div class="alternative">${candidateSummary(candidate, false)}<div class="review-card__action">${expansionCandidateAction(song, candidate)}</div></div>`).join("")}</details>` : ""}
    </article>`;
  }

  function expansionCandidateAction(song, candidate) {
    if (candidate.selected) return `<button class="button button--secondary" type="button" data-expansion-selection="clear" data-artist="${escapeHtml(candidate.artist_norm)}" data-candidate="${candidate.candidate_id}">Clear choice</button>`;
    if (!candidate.selectable) return `<button class="button button--secondary" type="button" disabled>Unavailable</button>`;
    const possible = candidate.result_tier === "POSSIBLE_MATCH";
    return `<button class="button button--secondary" type="button" data-expansion-selection="select" data-artist="${escapeHtml(candidate.artist_norm)}" data-candidate="${candidate.candidate_id}" data-possible="${possible}">${possible ? "Review and select" : "Select"}</button>`;
  }

  function bindExpansionActions() {
    $$('[data-expansion-selection]').forEach(button => button.addEventListener("click", async () => {
      setButtonBusy(button, true, "Saving...");
      try {
        const possible = button.dataset.possible === "true";
        if (possible && !window.confirm("This possible expansion match needs explicit confirmation. Select it?")) { setButtonBusy(button, false); return; }
        await api("/api/selection", {method: "POST", body: JSON.stringify({
          kind: "expansion",
          artist_norm: button.dataset.artist,
          candidate_id: Number(button.dataset.candidate),
          action: button.dataset.expansionSelection,
          confirm_possible: possible,
        })});
        state.selectionMode = "custom";
        await loadExpansion();
      } catch (error) {
        showError(error.message);
        setButtonBusy(button, false);
      }
    }));
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
    state.expansionJobId = Number(jobId);
    if (state.expansionPollTimer) window.clearInterval(state.expansionPollTimer);
    pollExpansionJob();
    state.expansionPollTimer = window.setInterval(pollExpansionJob, 1200);
  }

  async function pollExpansionJob() {
    const status = $("#expansionStatus");
    try {
      const {job} = await api(`/api/jobs/${state.expansionJobId}`);
      const done = Number(job.progress?.completed_artists || 0);
      const total = Number(job.progress?.total_artists || 0);
      status.textContent = job.status === "queued"
        ? "Expansion is queued and will start shortly."
        : job.message || (total ? `${done} of ${total} artists prepared.` : "Preparing artist recommendations...");
      if (isTerminal(job)) {
        window.clearInterval(state.expansionPollTimer);
        state.expansionPollTimer = null;
        state.expansionJobId = null;
        if (job.status !== "complete") showError(job.error || "Artist expansion did not finish.");
        await loadExpansion();
      }
    } catch (error) {
      window.clearInterval(state.expansionPollTimer);
      state.expansionPollTimer = null;
      showError(error.message);
    }
  }

  function openDownload() {
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
    $("#soundcloudForm")?.addEventListener("submit", importAuthenticatedSoundCloud);
    $("#selectAllPlaylistsButton")?.addEventListener("click", () => setAllSpotifyPlaylists(true));
    $("#clearPlaylistsButton")?.addEventListener("click", () => setAllSpotifyPlaylists(false));
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
