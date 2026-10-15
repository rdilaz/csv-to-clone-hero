# ZIP progress and workflow navigation

The released packaging screen could keep displaying the same chart count while
the server wrote the ZIP. `Writing ZIP archive` was emitted once before the
copy loop, with the completed validation count. The browser's "Last checked"
time described a status poll, not archive activity.

The local correction separates chart checking, retrieval/validation, ZIP
packaging, and finalization. Packaging reports measured files and source bytes
copied; these are neither the compressed archive size nor bytes transferred to
the user's browser. An unavailable denominator remains indeterminate. Actual
counter advancement, worker reports, and browser checks have separate times.
Cancellation is checked during the chunked ZIP copy and before publication.

The workflow steps and Back/Next controls navigate saved panels. Navigation
does not start a search, prepare another ZIP, cancel a job, or fetch a signed
single-use download link. An existing preparation remains visible in a compact
status panel with return and read-only status actions. Its saved selection is
preserved; music and chart mutations are paused while preparation is active or
its outcome is unknown. The listening player remains mounted between steps.

## Operational evidence and rollout boundary

Read-only inspection on 2026-10-15 found preparation 24 already cancelled at
21:52:45 UTC. Its saved 5,856/5,856 count described chart processing; its
selection contained 5,909 entries. No queued/running jobs, temporary ZIPs, or
staging directories remained in either subsequent resource snapshot. There was
no evidence of storage exhaustion. Because inspection followed cancellation
and cleanup, it cannot establish whether earlier packaging was slow or stalled.
The inspection did not cancel, restart, or prepare anything.

This change is prepared in an isolated local checkout based on
`23af904663e3a375ddf5a16332d3cb465e8ed4a5`. The released checkout, existing
previews, production service, and real Songs library are preserved. Deployment
requires a fresh active-job check and a coordinated safe transition. Test
screenshots use synthetic local API responses and do not demonstrate a real
multi-thousand-chart transfer.
