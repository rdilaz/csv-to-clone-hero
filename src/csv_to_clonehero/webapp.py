"""Simple local web interface.

Runs on 127.0.0.1 only. Lets a nontechnical user:

1. get CSVs (links to Chosic / Exportify),
2. upload them into input_playlists,
3. run a dry run or a full build with on-screen progress,
4. read the summary and download reports.

No terminal required.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

from flask import Flask, abort, jsonify, request, send_file

from csv_to_clonehero.config import AppConfig
from csv_to_clonehero.database import Database
from csv_to_clonehero.filesystem import sanitize_component
from csv_to_clonehero.pipeline import (
    run_downloads,
    run_plan,
    run_scan,
    run_search,
)
from csv_to_clonehero.reporting import generate_reports
from csv_to_clonehero.sources import build_sources
from csv_to_clonehero.sources.base import ChartSource

log = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 50 * 1024 * 1024


class JobState:
    """Shared state for the single background build job."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self.running = False
        self.action = ""
        self.stage = ""
        self.stages: list[dict] = []
        self.error = ""
        self.finished_at: float | None = None

    def begin_stage(self, label: str) -> None:
        with self.lock:
            self.stage = label
            self.stages.append({"label": label, "status": "running", "detail": ""})

    def end_stage(self, detail: str = "") -> None:
        with self.lock:
            if self.stages:
                self.stages[-1]["status"] = "done"
                self.stages[-1]["detail"] = detail

    def progress(self, done: int, total: int) -> None:
        with self.lock:
            if self.stages:
                self.stages[-1]["detail"] = f"{done} / {total}"

    def fail(self, message: str) -> None:
        with self.lock:
            if self.stages:
                self.stages[-1]["status"] = "failed"
            self.error = message
            self.running = False
            self.finished_at = time.time()

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "running": self.running,
                "action": self.action,
                "stage": self.stage,
                "stages": list(self.stages),
                "error": self.error,
                "finished_at": self.finished_at,
            }


def _run_job(
    config: AppConfig,
    state: JobState,
    action: str,
    source_factory: Callable[[AppConfig], list[ChartSource]],
) -> None:
    """Background worker: runs the pipeline, updating state as it goes."""
    dry_run = action == "dry-run"
    db = Database(config.database_path)
    sources: list[ChartSource] = []
    try:
        from csv_to_clonehero.overrides import load_overrides

        overrides = load_overrides(config.base_dir)
        sources = source_factory(config)

        state.begin_stage("Reading Spotify playlists")
        scan = run_scan(config, db, overrides)
        state.end_stage(
            f"{scan.csv_files} CSVs, {scan.total_rows} rows, "
            f"{scan.unique_tracks} unique tracks, {scan.primary_artists} primary artists"
        )

        state.begin_stage("Searching chart sources")
        search = run_search(config, db, sources, overrides, on_progress=state.progress)
        state.end_stage(
            f"{search.auto} auto matches, {search.review} review, "
            f"{search.no_match} no match"
            + (f", {search.errors} errors" if search.errors else "")
        )

        state.begin_stage("Planning library")
        plan = run_plan(config, db, sources, on_progress=state.progress)
        state.end_stage(
            f"{plan.exact_selected} exact + {plan.expansion_selected} expansion, "
            f"{plan.planned_bytes / (1024**3):.1f} GiB estimated"
        )

        if dry_run:
            state.begin_stage("Downloading charts")
            state.end_stage("skipped (dry run)")
        else:
            state.begin_stage("Downloading charts (this can take a while)")
            downloaded, errors, bytes_total = run_downloads(
                config, db, sources, on_progress=state.progress
            )
            state.end_stage(
                f"{downloaded} downloaded ({bytes_total / (1024**3):.2f} GiB)"
                + (f", {errors} errors" if errors else "")
            )

        state.begin_stage("Writing reports")
        generate_reports(config, db)
        state.end_stage(str(config.reports_path))

        with state.lock:
            state.running = False
            state.finished_at = time.time()
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI
        log.exception("build job failed")
        state.fail(str(exc))
    finally:
        for source in sources:
            source.close()
        db.close()


def create_app(
    config: AppConfig,
    source_factory: Callable[[AppConfig], list[ChartSource]] | None = None,
) -> Flask:
    """Create the Flask app. ``source_factory`` is injectable for tests."""
    if source_factory is None:
        source_factory = build_sources

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
    state = JobState()

    def _start_job(action: str) -> tuple[dict, int]:
        with state.lock:
            if state.running:
                return {"error": "a job is already running"}, 409
            state.reset()
            state.running = True
            state.action = action
        thread = threading.Thread(
            target=_run_job,
            args=(config, state, action, source_factory),
            daemon=True,
        )
        thread.start()
        return {"started": action}, 202

    # ------------------------------------------------------------- pages
    @app.get("/")
    def index():
        return _PAGE.replace("__LIBRARY_ROOT__", str(config.library_root))

    # -------------------------------------------------------------- files
    @app.get("/api/files")
    def list_files():
        input_dir = config.input_path
        files = []
        if input_dir.exists():
            for path in sorted(input_dir.glob("*.csv")):
                files.append({"name": path.name, "size": path.stat().st_size})
        return jsonify(files)

    @app.post("/api/upload")
    def upload():
        uploaded = request.files.getlist("files")
        if not uploaded:
            return {"error": "no files provided"}, 400
        input_dir = config.input_path
        input_dir.mkdir(parents=True, exist_ok=True)
        saved, rejected = [], []
        for item in uploaded:
            name = Path(item.filename or "").name
            if not name.lower().endswith(".csv"):
                rejected.append(name)
                continue
            safe_name = sanitize_component(Path(name).stem, fallback="playlist") + ".csv"
            item.save(input_dir / safe_name)
            saved.append(safe_name)
        return jsonify({"saved": saved, "rejected": rejected})

    @app.post("/api/files/delete")
    def delete_file():
        name = Path((request.json or {}).get("name", "")).name  # strip any path parts
        target = (config.input_path / name).resolve()
        if not str(target).startswith(str(config.input_path.resolve())):
            abort(403)
        if target.exists() and target.suffix.lower() == ".csv":
            target.unlink()
        return jsonify({"deleted": name})

    # --------------------------------------------------------------- jobs
    @app.post("/api/run/<action>")
    def run(action: str):
        if action not in ("dry-run", "build"):
            abort(404)
        return _start_job(action)

    @app.get("/api/status")
    def status():
        return jsonify(state.snapshot())

    # ------------------------------------------------------------ reports
    @app.get("/api/summary")
    def summary():
        path = config.reports_path / "summary.json"
        if not path.exists():
            return jsonify(None)
        return app.response_class(
            path.read_text(encoding="utf-8"), mimetype="application/json"
        )

    @app.get("/reports/<name>")
    def report_file(name: str):
        target = (config.reports_path / Path(name).name).resolve()
        if not str(target).startswith(str(config.reports_path.resolve())):
            abort(403)
        if not target.exists():
            abort(404)
        return send_file(target, as_attachment=True)

    @app.get("/api/reports")
    def list_reports():
        reports_dir = config.reports_path
        files = []
        if reports_dir.exists():
            for path in sorted(reports_dir.iterdir()):
                if path.is_file():
                    files.append(path.name)
        return jsonify(files)

    return app


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CSV to Clone Hero</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { font-family: "Segoe UI", system-ui, sans-serif; background: #12151c;
         color: #e6e9ef; margin: 0; }
  main { max-width: 760px; margin: 0 auto; padding: 24px 16px 64px; }
  h1 { font-size: 1.6rem; margin: 8px 0 4px; }
  h2 { font-size: 1.05rem; margin: 28px 0 8px; color: #8fd3ff; }
  .card { background: #1b2130; border: 1px solid #2a3247; border-radius: 10px;
          padding: 16px; margin-top: 10px; }
  .muted { color: #9aa4b8; font-size: .9rem; }
  a { color: #8fd3ff; }
  button { font: inherit; border: 0; border-radius: 8px; padding: 10px 18px;
           cursor: pointer; background: #2f3a56; color: #e6e9ef; }
  button.primary { background: #2f7d4f; }
  button:disabled { opacity: .45; cursor: default; }
  .file-row { display: flex; justify-content: space-between; align-items: center;
              padding: 6px 4px; border-bottom: 1px solid #2a3247; }
  .file-row button { padding: 4px 10px; background: #54303a; font-size: .85rem; }
  .stage { padding: 4px 0; }
  .stage .ok { color: #6fd08c; }
  .stage .bad { color: #ff7a7a; }
  .stage .spin { color: #ffd479; }
  table { border-collapse: collapse; margin-top: 6px; }
  td { padding: 3px 14px 3px 0; }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  #drop { border: 2px dashed #3a4456; border-radius: 10px; }
  #drop.over { border-color: #8fd3ff; background: #20293d; }
  .reports a { display: inline-block; margin: 2px 10px 2px 0; font-size: .9rem; }
  code { background: #0d1017; padding: 1px 6px; border-radius: 5px; }
</style>
</head>
<body>
<main>
  <h1>🎸 CSV to Clone Hero</h1>
  <p class="muted">Turn Spotify playlist exports into a Clone Hero library.
     Library folder: <code>__LIBRARY_ROOT__</code></p>

  <h2>1 · Get your playlist CSVs</h2>
  <div class="card">
    Export each public playlist to CSV with one of these free tools, then come back:
    <ul>
      <li><a href="https://www.chosic.com/spotify-playlist-exporter/" target="_blank" rel="noopener">Chosic Spotify Playlist Exporter</a> — log in with Spotify, pick a playlist, download CSV</li>
      <li><a href="https://exportify.net/" target="_blank" rel="noopener">Exportify</a> — log in with Spotify, export playlists to CSV</li>
    </ul>
  </div>

  <h2>2 · Add your CSVs</h2>
  <div class="card" id="drop">
    <input type="file" id="picker" accept=".csv" multiple>
    <span class="muted">or drag &amp; drop CSV files anywhere on this box</span>
    <div id="files"></div>
  </div>

  <h2>3 · Build the library</h2>
  <div class="card">
    <button id="dry" class="primary">🔍 Dry run (nothing downloaded)</button>
    <button id="build">⬇️ Build library (downloads charts)</button>
    <div id="stages" style="margin-top:12px"></div>
  </div>

  <h2>4 · Results</h2>
  <div class="card">
    <div id="summary" class="muted">No results yet — run a dry run first.</div>
    <div class="reports" id="reports" style="margin-top:10px"></div>
  </div>
</main>
<script>
const $ = (id) => document.getElementById(id);

async function refreshFiles() {
  const files = await (await fetch('/api/files')).json();
  $('files').innerHTML = files.length
    ? files.map(f => `<div class="file-row"><span>📄 ${f.name}
        <span class="muted">(${(f.size/1024).toFixed(0)} KB)</span></span>
        <button onclick="delFile('${f.name.replace(/'/g, "\\'")}')">remove</button></div>`).join('')
    : '<p class="muted">No CSVs yet.</p>';
}

async function delFile(name) {
  await fetch('/api/files/delete', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name})});
  refreshFiles();
}

async function upload(fileList) {
  const data = new FormData();
  for (const f of fileList) data.append('files', f);
  const res = await (await fetch('/api/upload', {method: 'POST', body: data})).json();
  if (res.rejected && res.rejected.length)
    alert('Skipped (not .csv): ' + res.rejected.join(', '));
  refreshFiles();
}

$('picker').addEventListener('change', e => upload(e.target.files));
const drop = $('drop');
['dragover','dragenter'].forEach(ev => drop.addEventListener(ev, e => {e.preventDefault(); drop.classList.add('over');}));
['dragleave','drop'].forEach(ev => drop.addEventListener(ev, e => {e.preventDefault(); drop.classList.remove('over');}));
drop.addEventListener('drop', e => upload(e.dataTransfer.files));

async function run(action) {
  $('dry').disabled = $('build').disabled = true;
  $('stages').innerHTML = '';
  const res = await fetch('/api/run/' + action, {method: 'POST'});
  if (res.status === 409) { alert('A job is already running.'); }
  poll();
}

async function poll() {
  const s = await (await fetch('/api/status')).json();
  $('stages').innerHTML = s.stages.map(st => {
    const icon = st.status === 'done' ? '<span class="ok">✔</span>'
               : st.status === 'failed' ? '<span class="bad">✖</span>'
               : '<span class="spin">…</span>';
    return `<div class="stage">${icon} ${st.label}
      <span class="muted">${st.detail || ''}</span></div>`;
  }).join('') + (s.error ? `<div class="stage bad" style="color:#ff7a7a">Error: ${s.error}</div>` : '');
  if (s.running) { setTimeout(poll, 700); }
  else {
    $('dry').disabled = $('build').disabled = false;
    refreshSummary(); refreshReports();
  }
}

async function refreshSummary() {
  const s = await (await fetch('/api/summary')).json();
  if (!s) return;
  $('summary').innerHTML = `<table>
    <tr><td>Playlist CSVs</td><td class="num">${s.csv_files}</td></tr>
    <tr><td>Unique tracks</td><td class="num">${s.unique_tracks}</td></tr>
    <tr><td>Primary artists</td><td class="num">${s.primary_artists}</td></tr>
    <tr><td>Exact: auto / review / no match</td>
        <td class="num">${s.exact.auto_matches} / ${s.exact.review} / ${s.exact.no_match}</td></tr>
    <tr><td>Expansion selected</td><td class="num">${s.expansion.selected}</td></tr>
    <tr><td>Estimated library size</td><td class="num">${s.plan.estimated_gib} GiB</td></tr>
    <tr><td>Downloaded</td><td class="num">${s.downloads.completed} (${s.downloads.gib_downloaded} GiB)</td></tr>
  </table>`;
}

async function refreshReports() {
  const files = await (await fetch('/api/reports')).json();
  $('reports').innerHTML = files.length
    ? 'Reports: ' + files.map(f => `<a href="/reports/${f}">${f}</a>`).join('')
    : '';
}

$('dry').addEventListener('click', () => run('dry-run'));
$('build').addEventListener('click', () => {
  if (confirm('Download charts into the library now?')) run('build');
});

refreshFiles(); refreshSummary(); refreshReports();
fetch('/api/status').then(r => r.json()).then(s => { if (s.running) poll(); });
</script>
</body>
</html>
"""
