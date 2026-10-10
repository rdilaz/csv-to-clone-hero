# Outcome Report (TL;DR)

Riff now gives an explicit recovery path when a search or ZIP preparation response is lost, previews bulk chart replacement before saving, and restores prior customized choices through guarded undo. Catalog readiness runs as an observable background job. Public ZIP delivery and local export retain separate ownership and filesystem boundaries.

This is the review candidate built from Music Studio commit aadc113fa40b917861990529957fc53a1d53c2ef, with public assets identified as riff-ux-20261010, local assets as 2.6.0-ux, and additive SQLite schema 22. No production deployment or merge is included. The original screenshot establishes a lost-resource symptom; the underlying hosted network failure remains unverified.

## Cognitive Load & Hierarchy

- **Jakob’s Law:** Review ZIP opens review, Prepare ZIP starts preparation, and Download ZIP requests browser delivery. The local surface names export to the Riff host and writing charts to its destination.
- **Fitts’s Law:** Status checks are full rectangular buttons within the affected panel. Existing large targets and the mobile selection dock are preserved. Effective 44×44 CSS pixels are the product goal; physical-phone reach remains unverified.
- **Hick’s Law:** Song pool, chart-version policy and explicit possible-match inclusion precede Apply selection. A server preview states all-page song/chart counts and replacement scope. One chart per song and exclusion of possible matches are the defaults.
- **Miller’s Law:** Download retains last confirmed selection counts with a timestamp when known. An unconfirmed current total reads Awaiting confirmation, with an explanation; separate imported-song and artist-expansion counts remain labeled. Packaged-file counts come from the verified delivery receipt.
- **Progressive Disclosure:** Song groups retain chart alternatives and difficulty evidence. Native fetch errors move into collapsed Technical details; uncertainty and the next recovery action remain exposed.

## Visual Design & Structure

- **Gestalt Principles:** The preparation heading, outcome explanation, last successful check and recovery actions share one panel. Selected charts remain a separate aligned ledger. Search and expansion recovery each stay with their own operation.
- **Contrast / Accessibility — WCAG 2.2:** Public and local essential control boundaries are strengthened, while the dark studio palette and readable text are preserved. Acceptance goals are **4.5:1 normal text**, **3:1 large text** under [SC 1.4.3](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html), and **3:1 essential control/state graphics against adjacent colors** under [SC 1.4.11](https://www.w3.org/WAI/WCAG22/Understanding/non-text-contrast.html). Large text means at least 18pt regular or 14pt bold. Disabled controls have the applicable contrast exemptions; explanations remain readable. [SC 2.5.8](https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html) specifies 24×24 CSS pixels or an applicable exception, distinct from the 44×44 product goal. Browser checks cover reflow, focus and reduced motion; this release does not claim WCAG certification.
- **Visual Hierarchy:** The Download headline describes preparation state instead of keeping an oversized slogan during failure. Check preparation status becomes the strongest available action when the outcome is unknown.
- **Spatial Consistency / Grids:** Notifications, stage content and selection tray use the same public shell width. Existing grouped song rows and narrow-screen stacking are preserved. Local export uses its own host-capability panel.
- **Affordance:** Unknown is text, rather than spinner-like dots. Busy, stale and unavailable controls have distinct explanations. Actions use modest rectangular corners and native checkbox/select semantics.

## Feedback & Interaction

- **Status of System:** ZIP polling uses bounded requests and at most three automatic read-only reconnection attempts before requiring a manual check. A known job’s last checkpoint is labeled stale when its status cannot be read. Search and expansion expose same-job status checks without automatically creating replacements.
- **Error Prevention:** Bulk previews and saved choices carry selection revision and exact-generation guards. Possible-match inclusion is deliberate; existing hard identity/artifact gates remain. Local cohort replacement changes choices in one transaction instead of clear-then-select requests. Unknown preparation responses do not trigger another preparation POST or probe the single-use ZIP URL.
- **Forgiveness / Reversibility:** Public history stores bounded owner-workspace snapshots and operation receipts for individual edits, clear/bulk changes and artist-expansion replacement. Undo restores customized candidate choices, with current revision/generation, job and artifact eligibility checks. Authorized edits still complete when the prior selection exceeds the restore budget; preview and the saved receipt explicitly report undo unavailable. No-op edits retain existing history. Local undo is separate, bounded, expiring local state. Lost undo responses require reconciliation rather than automatic retry.
- **Acceleration / Flexibility:** All-page bulk selection, lazily loaded 100-song pages and seeded shuffle remain. The deck shows Song X of N and nearby keyboard hints. Keyboard actions retain input-focus guards. A 100-song page is not a library limit; expansion previews remain unavailable unless recording identity is verified.
- **Consistency & Standards:** Selection uses eligible chart entries before file inspection and verified playable files afterward. Public browser ZIP and local export to this host are explicit. Native local picker capability is surfaced before interaction; no public native-folder capability or cross-surface state synchronization is introduced.

## Behavior & Psychology

- **Peak-End Rule:** Public completion says Your ZIP is ready and changes to Download requested on native handoff. Browser save completion is not inferred. Local Done uses the successful export receipt, destination and subsequent Scan Songs instruction for Clone Hero/YARG.
- **Zeigarnik Effect:** Saved choices, a running preparation job and a usable ZIP are separate checkpoints. Reload recovery reads the saved job; a restored readiness check does not automatically start discovery. Expired, consumed or missing delivery state uses the existing guarded recovery flow.
- **Aesthetic-Usability Effect:** The decorative WebGL scene, fallback and independent motion preference remain. Reduced motion does not stop job status reads. Decorative movement is not presented as proof of matching or packaging progress.
- **Von Restorff Effect:** Lime highlights the next enabled action and a thin current-step marker; violet remains structural. Operational panels use solid dark surfaces and textual state labels. No pill action controls or generic purple/indigo accent gradients are added.
- **Doherty Threshold:** Public POST preflight promptly queues an observable readiness job; the worker performs expensive catalog validation or bounded fallback checks. Its GET is a cheap cache/job read. The local pipeline emits catalog preparation before loading. Neither surface invents a percentage or ETA before a denominator exists. Sub-100ms input feedback and roughly 400ms lightweight interaction are design budgets, not measured provider guarantees.

## The complete journey and service boundaries

- **Add Music:** Public provider imports keep existing owner sessions and saved scopes; readiness failure preserves those choices. Local imports and folder inventory stay in local state.
- **Find Charts:** Public readiness and discovery are separate durable jobs, with catalog-first discovery and existing bounded fallback. Local catalog preparation is reported before loading; an unavailable catalog is a local repair condition.
- **Build Setlist:** Public all-page preview, explicit uncertainty, guarded changes and global undo operate on the owner’s workspace. Local eligible/strong cohort replacement and local undo are atomic local operations.
- **Download:** Public prepares and validates a ZIP, then hands off its signed single-use link through native browser navigation. Local writes only to the authorized Riff host destination and explains host capabilities.
- **Done:** Each surface displays its verified receipt and the remaining browser/game action. Neither claims successful game installation from an earlier checkpoint.

## Verification and operational limits

The accompanying implementation report and evidence record the exact final test counts and source hashes. Backend tests use isolated SQLite databases and fake chart sources. Browser tests exercise the real templates and scripts against synthetic API fixtures with provider traffic blocked. Phone/tablet/desktop layouts, failure recovery, reload, concurrent/stale edits, customized undo, reduced motion and ZIP validation are included where specified by those reports.

Schema 22 is additive. Restore history is workspace-scoped; shared chart identities/cache and the process-wide worker remain shared intentionally. Public operation history is bounded to ten retained receipts, 32 MiB per snapshot and 64 MiB per workspace across retained receipts; local restore limits and expiry are documented in the implementation report. A restart can interrupt jobs or invalidate the in-memory signed-download registry under the existing architecture. These changes do not introduce resumable byte transfers, new workers, a new database service, or production configuration changes.

Real Spotify/Enchor acceptance, actual multi-gigabyte archives, production network root cause, physical devices, and full accessibility conformance remain unverified. The original source, synced references and production state are preserved.

Self-audit: exactly twenty principles above, each with a structural outcome; all five journey stages are covered; public/local boundaries and exact WCAG contrast goals are explicit.
