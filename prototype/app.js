(function () {
  "use strict";

  const fixtures = window.PROTOTYPE_FIXTURES;
  const params = new URLSearchParams(window.location.search);
  const initialState = fixtures.states.includes(params.get("state"))
    ? params.get("state")
    : "music-ready";

  const model = {
    state: initialState,
    terminal: params.get("terminal") === "partial" ? "partial" : "complete",
    selected: initialState === "choices-strong-selected" || initialState.startsWith("build-"),
    reviewChoice: "",
    mobileReviewPane: params.get("pane") === "queue" ? "queue" : "decision",
    detailsOpen: false
  };

  if (params.get("clean") === "1") document.body.classList.add("clean-shot");

  const app = document.getElementById("app");
  const announcer = document.getElementById("announcer");

  const icons = {
    music: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 18V5l10-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="16" cy="16" r="3"/></svg>',
    choices: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6"/><path d="M5 6h6M5 18h6"/></svg>',
    build: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v12m0 0 5-5m-5 5-5-5"/><path d="M5 19h14"/></svg>',
    arrow: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg>',
    check: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6"/></svg>',
    search: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></svg>',
    folder: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7h7l2 2h9v10H3z"/></svg>',
    pause: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14M16 5v14"/></svg>',
    undo: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 7 4 12l5 5"/><path d="M5 12h8a6 6 0 0 1 6 6"/></svg>'
  };

  function number(value) {
    return Number(value).toLocaleString("en-US");
  }

  function announce(message) {
    announcer.textContent = "";
    window.setTimeout(function () { announcer.textContent = message; }, 20);
  }

  function workspaceFor(state) {
    if (state.startsWith("music")) return "music";
    if (state.startsWith("build")) return "build";
    return "choices";
  }

  function navButton(id, label, icon, workspace) {
    const current = workspace === id;
    const count = id === "build" && model.selected ? '<span class="nav-count">568</span>' : "";
    return '<button class="nav-item' + (current ? " is-active" : "") + '" data-nav="' + id + '"' +
      (current ? ' aria-current="page"' : "") + '>' + icon + '<span>' + label + '</span>' + count + '</button>';
  }

  function shell(content) {
    const workspace = workspaceFor(model.state);
    const operation = operationPill();
    return '<div class="app-shell">' +
      '<aside class="rail" aria-label="Primary navigation">' +
        '<div class="brand" aria-label="Riff Library"><span class="brand-mark"><i></i><i></i><i></i></span><span>Riff Library</span></div>' +
        '<nav class="rail-nav">' +
          navButton("music", "Music", icons.music, workspace) +
          navButton("choices", "Chart Choices", icons.choices, workspace) +
          navButton("build", "Build", icons.build, workspace) +
        '</nav>' +
        '<div class="rail-foot"><button class="quiet-link" data-action="show-help">Help & details</button></div>' +
      '</aside>' +
      '<main id="main-content" class="main-content" tabindex="-1">' + operation + content + '</main>' +
      '<nav class="bottom-nav" aria-label="Primary navigation">' +
        navButton("music", "Music", icons.music, workspace) +
        navButton("choices", "Choices", icons.choices, workspace) +
        navButton("build", "Build", icons.build, workspace) +
      '</nav>' +
      fixtureTools() +
    '</div>';
  }

  function fixtureTools() {
    const options = fixtures.states.map(function (state) {
      return '<option value="' + state + '"' + (state === model.state ? " selected" : "") + '>' + state + '</option>';
    }).join("");
    const terminal = model.state === "build-terminal"
      ? '<select id="terminal-switch" aria-label="Build terminal fixture"><option value="complete"' + (model.terminal === "complete" ? " selected" : "") + '>complete</option><option value="partial"' + (model.terminal === "partial" ? " selected" : "") + '>partial</option></select>'
      : "";
    return '<div class="fixture-tools"><span>Prototype state</span><select id="fixture-switch" aria-label="Prototype state">' + options + '</select>' + terminal + '</div>';
  }

  function operationPill() {
    if (model.state === "matching-running") {
      return '<button class="operation-pill" data-action="view-operation"><span class="activity-dot"></span><span><strong>Finding charts</strong><small>3,470 of 5,031 checked</small></span>' + icons.arrow + '</button>';
    }
    if (model.state === "build-running") {
      return '<button class="operation-pill" data-action="view-operation"><span class="activity-dot"></span><span><strong>Building library</strong><small>284 of 568 ready</small></span>' + icons.arrow + '</button>';
    }
    return "";
  }

  function eyebrow(text) {
    return '<p class="eyebrow">' + text + '</p>';
  }

  function cover(art, className) {
    return '<div class="album-art ' + art + (className ? " " + className : "") + '" aria-hidden="true"><span></span></div>';
  }

  function artMosaic() {
    return '<div class="art-mosaic" aria-hidden="true">' +
      cover("cover-1") + cover("cover-4") + cover("cover-2") + cover("cover-5") + cover("cover-3") +
      '<div class="mosaic-glow"></div></div>';
  }

  function primaryButton(label, action, extra) {
    return '<button class="button primary" data-action="' + action + '">' + label + (extra || "") + '</button>';
  }

  function musicEmpty() {
    return shell('<section class="empty-stage">' +
      '<div class="empty-art">' + artMosaic() + '</div>' +
      '<div class="empty-copy">' + eyebrow("Music") +
        '<h1>Build a playable library from music you already love.</h1>' +
        '<p>Bring your Spotify and SoundCloud favorites together, then turn the best matches into Clone Hero charts.</p>' +
        primaryButton("Add music", "add-music", '<span aria-hidden="true">+</span>') +
      '</div></section>');
  }

  function musicReady() {
    const rows = fixtures.musicSample.map(function (song) {
      return '<li class="song-row">' + cover(song.art, "art-small") +
        '<span class="song-copy"><strong>' + song.title + '</strong><small>' + song.artist + '</small></span>' +
        '<span class="source-label">' + song.source + '</span>' +
        '<button class="icon-button" aria-label="More options for ' + song.title + '">&middot;&middot;&middot;</button></li>';
    }).join("");
    return shell('<header class="workspace-head">' + eyebrow("Music") +
      '<button class="button secondary compact" data-action="add-music">Add music</button></header>' +
      '<section class="music-hero">' +
        '<div class="hero-copy"><h1><span class="big-number">5,031</span> unique songs</h1>' +
        '<p>Your Spotify and SoundCloud music, combined.</p>' +
        '<div class="source-breakdown" aria-label="Music sources"><span><strong>2,819</strong> Spotify</span><span><strong>2,354</strong> SoundCloud</span><span><strong>142</strong> in both</span></div>' +
        primaryButton("Find playable charts", "find-charts", icons.arrow) + '</div>' +
        '<div class="hero-art">' + artMosaic() + '</div></section>' +
      '<section class="library-sample" aria-labelledby="sample-heading"><div class="section-heading"><div><p class="eyebrow">A sample from your library</p><h2 id="sample-heading">Recently added</h2></div><button class="icon-button search-button" aria-label="Search music">' + icons.search + '</button></div>' +
        '<ul class="song-list">' + rows + '</ul><p class="sample-note">Showing 5 of 5,031 songs</p></section>');
  }

  function matchingRunning() {
    return shell('<header class="workspace-head">' + eyebrow("Chart Choices") + '</header>' +
      '<section class="progress-stage">' +
        '<div class="progress-orbit" aria-hidden="true"><span>69%</span><i></i></div>' +
        '<div class="progress-copy"><p class="status-kicker"><span class="activity-dot"></span> Finding playable charts</p>' +
          '<h1>Checking your songs</h1>' +
          '<p class="progress-count"><strong>3,470</strong> of 5,031 checked</p>' +
          '<div class="progress-track" role="progressbar" aria-label="Songs checked" aria-valuemin="0" aria-valuemax="5031" aria-valuenow="3470"><span style="width:69%"></span></div>' +
          '<p class="found-count"><strong>2,168</strong> playable options found so far</p>' +
          '<div class="current-song">' + cover("cover-1", "art-medium") + '<span><small>Current song</small><strong>James Tillman</strong><span>And Then&hellip;</span></span></div>' +
          '<div class="reassurance"><span class="reassurance-icon">' + icons.check + '</span><span><strong>No action needed.</strong><small>Your progress is saved.</small></span></div>' +
          '<details class="details"><summary>Details</summary><div><p>1,402 songs were restored from saved matching work.</p><p>Matching checks song identity, version details, instruments, and chart playability. No chart files are downloaded during this step.</p></div></details>' +
        '</div></section>');
  }

  function choicesComplete(selected) {
    if (selected) return choicesSelected();
    return shell('<header class="workspace-head">' + eyebrow("Chart Choices") + '<span class="updated-label">Updated just now</span></header>' +
      '<section class="choices-hero">' +
        '<div class="choices-copy"><p class="eyebrow">Matching complete</p><h1><span class="big-number">3,160</span> songs have playable chart options</h1>' +
          '<div class="strong-opportunity"><span class="vinyl-mark" aria-hidden="true"><i></i></span><div><p><strong>568</strong> strong recommendations</p><span>Artist, title, version, and playability checks all look good.</span></div></div>' +
          '<div class="button-row">' + primaryButton("Select 568 recommended charts", "select-strong", icons.check) + '<button class="button text-button" data-action="review-more">Review more matches</button></div>' +
          '<details class="details subdued-details"><summary>See why 1,871 songs have no recommendation</summary><div class="unavailable-summary"><p><strong>1,768</strong> had no suitable option</p><p><strong>12</strong> were not playable</p><p><strong>91</strong> had no chart candidate</p></div></details>' +
        '</div>' +
        '<div class="result-art" aria-hidden="true"><div class="record"><span></span></div><div class="result-number">3,160<small>playable</small></div></div>' +
      '</section>');
  }

  function choicesSelected() {
    return shell('<header class="workspace-head">' + eyebrow("Chart Choices") + '<span class="updated-label">Choices saved</span></header>' +
      '<section class="selected-stage">' +
        '<div class="selected-visual" aria-hidden="true"><div class="selected-ring">' + icons.check + '</div>' + artMosaic() + '</div>' +
        '<div class="selected-copy"><p class="eyebrow">Ready to build</p><h1><span class="big-number">568</span> charts selected</h1><p>You can build now or find more charts first.</p>' +
          '<div class="button-row">' + primaryButton("Build 568 charts", "go-build", icons.arrow) + '<button class="button secondary" data-action="review-more">Review more matches</button></div>' +
          '<button class="undo-change" data-action="undo-strong">' + icons.undo + '<span>568 strong recommendations selected</span><strong>Undo</strong></button>' +
        '</div></section>');
  }

  function reviewSets() {
    const sets = fixtures.reviewSets.map(function (set) {
      return '<article class="review-set">' + cover(set.art, "set-art") + '<div class="set-copy"><h2>' + set.title + '</h2><p>' + set.description + '</p></div>' +
        '<strong class="set-count">' + number(set.count) + '<small>songs</small></strong>' +
        '<button class="button secondary compact" data-action="review-session">Review 25</button></article>';
    }).join("");
    return shell('<header class="workspace-head">' + eyebrow("Chart Choices / Review") + '<span class="selected-chip">568 selected</span></header>' +
      '<section class="review-intro"><p class="eyebrow">Optional review</p><h1>Find more playable charts</h1><p>Choose a short review session. <strong>You never need to review all 2,592 songs.</strong></p></section>' +
      '<section class="review-set-list" aria-label="Review sets">' + sets + '</section>' +
      '<div class="review-footer"><p>Each session contains 25 songs. Stop whenever your library feels complete.</p><button class="button text-button" data-action="go-build">Build 568 charts instead</button></div>');
  }

  function queueMarkup() {
    const rows = fixtures.reviewQueue.map(function (song, index) {
      const marker = song.state === "done" ? icons.check : index + 1;
      return '<button class="queue-row ' + song.state + '" data-action="open-decision">' +
        '<span class="queue-marker">' + marker + '</span>' + cover(song.art, "art-tiny") +
        '<span><strong>' + song.title + '</strong><small>' + song.artist + '</small></span></button>';
    }).join("");
    return '<aside class="review-queue" aria-label="Review session queue"><div class="queue-head"><button class="back-link" data-action="back-sets">' + icons.arrow + ' Review sets</button><p><strong>Session 3 of 25</strong><span>2 decided</span></p><div class="mini-progress"><span style="width:8%"></span></div></div><div class="queue-list">' + rows + '</div><div class="queue-foot"><span>22 remaining</span><button data-action="finish-session">Finish session</button></div></aside>';
  }

  function decisionMarkup() {
    const review = fixtures.currentReview;
    const alternatives = review.alternatives.map(function (alt, index) {
      const chosen = model.reviewChoice === "alternative-" + index;
      return '<button class="alternative-row' + (chosen ? " is-selected" : "") + '" data-action="choose-alternative" data-index="' + index + '"><kbd>' + alt.key + '</kbd><span><strong>' + alt.title + '</strong><small>' + alt.meta + ' &middot; ' + alt.charter + '</small></span><span class="alternative-action">' + (chosen ? "Selected" : "Choose") + '</span></button>';
    }).join("");
    const recommendedSelected = model.reviewChoice === "recommended";
    return '<section class="decision-panel ' + (model.mobileReviewPane === "queue" ? "mobile-hidden" : "") + '">' +
      '<button class="mobile-queue-back" data-action="show-queue">' + icons.arrow + ' Session queue</button>' +
      '<div class="decision-scroll"><section class="your-song">' + eyebrow("Your song") + '<div>' + cover(review.art, "art-large") + '<span><h1>' + review.sourceTitle + '</h1><p>' + review.sourceArtist + '</p><small>' + review.source + '</small></span></div></section>' +
      '<section class="recommended-chart' + (recommendedSelected ? " is-selected" : "") + '"><div class="recommendation-head"><div><p class="eyebrow">Recommended chart</p><h2>' + review.recommended.title + '</h2><p>' + review.recommended.artist + '</p></div><span class="recommendation-state">' + (recommendedSelected ? icons.check + " Selected" : "Recommended") + '</span></div>' +
        '<div class="chart-meta"><span>Guitar</span><span>Bass</span><span>Drums</span><span>Charted by ' + review.recommended.charter + '</span></div>' +
        '<div class="concern"><span>Worth a look</span><p>' + review.recommended.concern + '</p></div>' +
        '<button class="button primary select-chart" data-action="select-recommended">' + (recommendedSelected ? "Selected" : "Select this chart") + '<kbd>A</kbd></button></section>' +
      '<section class="alternatives"><div class="section-heading"><div><p class="eyebrow">Other versions</p><h2>Compare alternatives</h2></div></div>' + alternatives + '</section>' +
      '<details class="details"><summary>Why this match?</summary><div><p>The artist closely matches your song and guitar is available. The recommended chart has stronger instrument coverage than the alternatives.</p></div></details></div>' +
      '<footer class="decision-actions"><button class="button quiet" data-action="decide-later">Decide later <kbd>D</kbd></button><button class="button quiet" data-action="skip-song">Don\'t add this song <kbd>S</kbd></button><button class="button quiet undo" data-action="undo-review"' + (model.reviewChoice ? "" : " disabled") + '>' + icons.undo + ' Undo <kbd>U</kbd></button><button class="button secondary finish" data-action="finish-session">Finish session</button></footer>' +
      '</section>';
  }

  function reviewFocus() {
    return shell('<header class="review-mobile-head">' + eyebrow("Chart Choices / Review") + '<span class="selected-chip">568 selected</span></header>' +
      '<section class="focus-review ' + (model.mobileReviewPane === "queue" ? "show-queue" : "show-decision") + '">' + queueMarkup() + decisionMarkup() + '</section>');
  }

  function buildReady() {
    return shell('<header class="workspace-head">' + eyebrow("Build") + '<span class="selected-chip">568 selected</span></header>' +
      '<section class="build-plan">' +
        '<div class="build-summary"><p class="eyebrow">Ready to build</p><h1><span class="big-number">568</span> selected charts</h1><p>Everything is ready for one final check.</p>' +
          '<dl class="build-facts"><div><dt>Already available</dt><dd>131</dd></div><div><dt>Need download</dt><dd>437</dd></div><div><dt>Approximate download</dt><dd>13.1 GB</dd></div></dl>' +
          primaryButton("Build 568 charts", "start-build", icons.arrow) +
        '</div>' +
        '<div class="destination-art"><div class="folder-visual">' + icons.folder + '<span><small>Destination</small><strong>Clone Hero / Songs</strong></span></div>' + artMosaic() + '</div>' +
      '</section>');
  }

  function buildRunning() {
    return shell('<header class="workspace-head">' + eyebrow("Build") + '</header>' +
      '<section class="build-progress">' +
        '<div class="build-progress-head"><div><p class="status-kicker"><span class="activity-dot"></span> In progress</p><h1>Building your library</h1><p><strong>284</strong> of 568 ready</p></div><div class="build-percent">50%</div></div>' +
        '<div class="download-track" role="progressbar" aria-label="Charts ready" aria-valuemin="0" aria-valuemax="568" aria-valuenow="284"><span style="width:50%"></span><i style="left:23%"></i></div>' +
        '<div class="build-counts"><span><strong>131</strong> reused</span><span><strong>153</strong> newly added</span><span><strong>284</strong> remaining</span></div>' +
        '<div class="now-downloading"><div class="download-art">' + cover("cover-6", "art-large") + '<span class="download-pulse"></span></div><div><small>Current chart</small><h2>Friday Morning</h2><p>Khruangbin</p><span class="download-status">Downloading and validating</span></div></div>' +
        '<div class="reassurance build-reassurance"><span class="reassurance-icon">' + icons.check + '</span><span><strong>No action needed.</strong><small>Completed charts are kept if the app closes.</small></span></div>' +
        '<button class="button quiet stop-button" data-action="stop-build">' + icons.pause + ' Stop after current chart</button>' +
      '</section>');
  }

  function buildTerminal() {
    const partial = model.terminal === "partial";
    const content = partial
      ? '<p class="eyebrow">Ready with a few issues</p><h1><span class="big-number">560</span> of 568 charts are ready</h1><p>Your playable library is available now. Eight charts can be retried when you are ready.</p><div class="issue-lines"><span><strong>5</strong> downloads failed</span><span><strong>3</strong> files did not pass validation</span></div><div class="button-row">' + primaryButton("Retry 8 unfinished charts", "retry-build", icons.arrow) + '<button class="button secondary" data-action="open-folder">' + icons.folder + ' Open Songs folder</button></div>'
      : '<p class="eyebrow">Build complete</p><h1><span class="big-number">568</span> charts are in your Songs folder</h1><p>Scan the Songs folder in Clone Hero or YARG before playing your newly added charts.</p><div class="button-row">' + primaryButton("Open Songs folder", "open-folder", icons.folder) + '</div><p class="completion-detail">437 added &middot; 131 already available</p>';
    return shell('<header class="workspace-head">' + eyebrow("Build") + '</header>' +
      '<section class="terminal-stage ' + (partial ? "is-partial" : "is-complete") + '"><div class="terminal-art">' + artMosaic() + '<div class="terminal-seal">' + (partial ? "560" : icons.check) + '</div></div><div class="terminal-copy">' + content + '</div></section>');
  }

  function render() {
    if (model.state === "music-empty") app.innerHTML = musicEmpty();
    else if (model.state === "music-ready") app.innerHTML = musicReady();
    else if (model.state === "matching-running") app.innerHTML = matchingRunning();
    else if (model.state === "choices-complete-unselected") app.innerHTML = choicesComplete(false);
    else if (model.state === "choices-strong-selected") app.innerHTML = choicesComplete(true);
    else if (model.state === "review-sets") app.innerHTML = reviewSets();
    else if (model.state === "review-focus") app.innerHTML = reviewFocus();
    else if (model.state === "build-ready") app.innerHTML = buildReady();
    else if (model.state === "build-running") app.innerHTML = buildRunning();
    else app.innerHTML = buildTerminal();
    bindControls();
  }

  function setState(state, message) {
    model.state = state;
    model.selected = state === "choices-strong-selected" || state.startsWith("build-") || model.selected;
    render();
    if (message) announce(message);
    window.scrollTo(0, 0);
  }

  function bindControls() {
    const switcher = document.getElementById("fixture-switch");
    if (switcher) switcher.addEventListener("change", function () {
      model.reviewChoice = "";
      model.mobileReviewPane = "decision";
      setState(switcher.value);
    });
    const terminal = document.getElementById("terminal-switch");
    if (terminal) terminal.addEventListener("change", function () {
      model.terminal = terminal.value;
      render();
    });
  }

  app.addEventListener("click", function (event) {
    const target = event.target.closest("[data-action], [data-nav]");
    if (!target) return;
    const nav = target.dataset.nav;
    if (nav === "music") return setState("music-ready");
    if (nav === "choices") return setState(model.selected ? "choices-strong-selected" : "choices-complete-unselected");
    if (nav === "build") return setState("build-ready");

    const action = target.dataset.action;
    if (action === "add-music") return setState("music-ready", "Music fixture loaded.");
    if (action === "find-charts") return setState("matching-running", "Matching started. No action is needed.");
    if (action === "view-operation") return setState(model.state);
    if (action === "select-strong") { model.selected = true; return setState("choices-strong-selected", "568 recommended charts selected."); }
    if (action === "undo-strong") { model.selected = false; return setState("choices-complete-unselected", "Selection undone."); }
    if (action === "review-more" || action === "back-sets") return setState("review-sets");
    if (action === "review-session" || action === "open-decision") { model.mobileReviewPane = "decision"; return setState("review-focus"); }
    if (action === "show-queue") { model.mobileReviewPane = "queue"; return render(); }
    if (action === "select-recommended") { model.reviewChoice = "recommended"; render(); return announce("Recommended chart selected."); }
    if (action === "choose-alternative") { model.reviewChoice = "alternative-" + target.dataset.index; render(); return announce("Alternative chart selected."); }
    if (action === "decide-later") return announce("Song left for a later session.");
    if (action === "skip-song") { model.reviewChoice = "skipped"; render(); return announce("This song will not be added."); }
    if (action === "undo-review") { model.reviewChoice = ""; render(); return announce("Last review decision undone."); }
    if (action === "finish-session" || action === "go-build") return setState("build-ready");
    if (action === "start-build" || action === "retry-build") return setState("build-running", "Build started. No action is needed.");
    if (action === "stop-build") return announce("Stop requested. The current chart will finish safely.");
    if (action === "open-folder") return announce("Prototype only: the Songs folder would open here.");
    if (action === "show-help") return announce("Help and diagnostic details are prototype-only.");
  });

  document.addEventListener("keydown", function (event) {
    if (model.state !== "review-focus") return;
    if (/INPUT|TEXTAREA|SELECT/.test(event.target.tagName)) return;
    const key = event.key.toLowerCase();
    if (key === "a") model.reviewChoice = "recommended";
    else if (["1", "2", "3"].includes(key)) model.reviewChoice = "alternative-" + (Number(key) - 1);
    else if (key === "d") return announce("Song left for a later session.");
    else if (key === "s") model.reviewChoice = "skipped";
    else if (key === "u") model.reviewChoice = "";
    else return;
    event.preventDefault();
    render();
    announce(key === "u" ? "Last review decision undone." : "Review choice saved.");
  });

  render();
}());
