# Signal Check: first human checkpoint

This branch implements only specification phases 0-3: the shared public shell,
Landing, Add Music, Find, and Confirm. Download, Done, artist expansion, and the
two post-confirm count corrections remain for phases 4-5 after approval.

## Isolation and baseline

- Branch: `riff/signal-check-ui-v1`
- Baseline: `ab64a590c1bb6652cf0b6823e36cfe1c87b02207`
- Product data: never used by the preview or browser tests
- Backend/matcher changes: none
- Baseline full offline suite: 700 passed, 2 failed, 5 deselected
- Checkpoint full offline suite: 719 passed, 2 failed, 5 deselected

The same two `tests/test_local_journey_e2e.py` cases fail before and after this
change because an isolated worktree intentionally has no untracked root
`config.yaml`. Focused public tests are green.

## Preview

From this worktree:

```powershell
& ".\.venv\Scripts\python.exe" -m tests.public_preview --scenario all --viewport 1440x900
```

The command opens Landing, Add Music, Find, and Confirm in real Chromium tabs.
It serves the production Flask templates, CSS, and JavaScript while intercepting
only provider/API requests with DTO-faithful fixtures. Its SQLite session
database and fixture state are temporary. Press `Ctrl+C` in the preview terminal
to stop the server and browser cleanly.

Use `--viewport 390x844` for the locked mobile baseline, or select one surface
with `--scenario landing`, `--scenario add`, `--scenario find`, or
`--scenario confirm`.

## Asset provenance

- IBM Plex Sans variable Latin-1 WOFF2, pinned to IBM/plex commit
  `bf260093582f04622aacc1e9f9ca604d7ccd0c42`.
- Space Grotesk variable WOFF2, pinned to floriankarsten/space-grotesk commit
  `03507d024a01282884232081fc6011c09ff4e849`.
- Exact SIL Open Font License text ships beside each font.
- `riff-wordmark.svg` and `signal-knot.svg` are original project assets.
- New initial-load art and font files total 121,570 bytes.

The package-data list was narrowly extended for `.svg`, `.woff2`, and `.txt`.
A built wheel was inspected and contained all ten required templates, scripts,
art assets, fonts, and license files.

## Verification

- `pytest tests/test_public_browser.py -q`: 20 passed in real Chromium.
- `pytest tests/test_server_web.py tests/test_server_downloads.py -q`: 79 passed.
- `pytest`: 719 passed, 2 baseline-only failures, 5 integration tests deselected.
- `python -m compileall -q src tests`: passed.
- `node --check src/csv_to_clonehero/server/static/public.js`: passed.
- `pip check`: passed.
- Both independent post-fix behavior and responsive reviews: passed.

## Proof comparison

- Landing preserves the approved dark-world composition, oversized interrupted
  wordmark, left porcelain action block, asymmetric violet/cyan knot, and lower
  three-part explainer.
- Find preserves the approved stepped rail, cropped knot field, single factual
  progress meter, large percentage, current phase, current song, calm leave
  note, detail disclosure, cancel action, and ETA.
- Confirm uses the corrected broad porcelain ledger with a narrow persistent
  selection rail. The fixture truthfully starts at three chart artifacts for
  one imported song and zero artist-expansion charts.
- Mobile keeps one DOM order, collapses selection presets, opens disclosures
  containing selected siblings, and places the first candidate action above
  the persistent bottom summary at 390x844. The first group starts within the
  380-pixel proof budget; an enabled support banner is separately verified at
  44 pixels total. Proof screenshots use the same disabled-banner treatment as
  the approved references.
