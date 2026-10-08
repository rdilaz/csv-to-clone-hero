# More playable songs and honest difficulty levels

## Counts

Imported songs searched, songs with recommendations, selectable chart versions,
selected charts and verified files delivered in a ZIP are different counts.
Paging changes only the visible song list. A search over thousands of imported
songs does not promise thousands of matching charts in the current snapshot.
The built-in chart source remains Chorus Encore; this change does not acquire
another provider's catalog or enlarge the installed metadata snapshot.

## Artist expansion

The additional-song panel supports two explicit modes. **All compatible songs**
keeps identity-compatible guitar charts even when their quality score is below
the recommendation threshold, and includes every compatible non-imported song
for the chosen artists. Quality still orders charts. **Top recommendations**
retains the existing quality threshold and the per-artist 10/25/50 song caps.

Choose artists individually, or opt into **All artists in my imported music**.
The server resolves that full eligible list from the owner's current completed
catalog-backed search in one job. Individual requests remain limited to 25
artists. The all-artist job does not issue provider searches. Starting expansion
again replaces the entire previous additional-song set; imported-song choices
stay separate.

**Select one chart per additional song** requires confirmation, rechecks the
current owner workspace/generation and compatibility, and preserves imported
choices. When coverage has been inspected, choices prefer charts with more
guitar difficulty levels. Unknown and Expert-only charts remain available.
The selection total is not a promise that every selected file will download.

Hard gates still exclude incompatible instruments, unsupported downloads,
severe chart issues, rejected artifacts and uncertain artist identities. Lower
quality cover/tribute/karaoke charts are not promoted by the all mode. Doubly
uncertain imported artist/title pairs cannot be auto-filled or manually
selected as plausible matches; independently trusted pair identity can still
support a match.

## Difficulty levels and actual notes

A numeric guitar difficulty is an **intensity rating**, not Easy/Medium/Hard/
Expert coverage. Riff displays **(Expert only)** only when current artifact
inspection found playable notes exclusively at Expert across all detected
parts. Mixed charts can instead say **(Guitar: Expert only)** while showing
other parts' easier levels. **Difficulty levels unknown** means the artifact
has not yet supplied trustworthy current note evidence; it stays selectable.

Before ZIP inclusion, downloaded `.sng` content must have real playable note
lanes. Text-chart force/tap markers and MIDI modifier notes do not count as
playable notes, and pro-guitar MIDI tracks do not masquerade as five-fret guitar.
Verified per-part note counts survive artifact validation and become visible
in subsequent chart review. Older verifier observations remain auditable, but
their missing or outdated coverage cannot claim an Expert-only label.

Inspected multi-level guitar charts are preferred within the same song and
identity tier in review and recommendation selection. This preference never
turns a wrong song into a match or removes a valid Expert-only chart.
