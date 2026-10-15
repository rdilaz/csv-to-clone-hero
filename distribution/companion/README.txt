RIFF COMPANION - LOCAL PREVIEW
Windows x64 / existing CPython 3.12 required

1. Extract the entire ZIP to a folder you control.
2. Double-click START_COMPANION.cmd. Open http://127.0.0.1:8947/ in a browser.
3. Keep the launcher terminal open. Ctrl+C stops the local server.

The ZIP includes the app and its licensed dependencies. It does not include or
install Python, music, a catalog snapshot, artwork, user data, or account keys.
No account, service, network download, or administrator permission is required.
This is a local browser tool, not a signed native desktop app or installer.

FIRST RUN / YOUR EXISTING DATA
The companion intentionally starts without music or a catalog. Use
CONFIGURE_COMPANION.cmd to choose your own existing checksum-valid Riff catalog
.jsonl snapshot, optional export-artifacts package cache folder, and optional
Songs folder to inspect read-only. No locations are discovered automatically.
Catalog snapshots and song packages are not provided by this distribution.
You must have permission to use the metadata, packages and audio you supply.

Settings live only in your user folder under .riff-companion/settings.json.
You can also launch with explicit arguments, for example:
  START_COMPANION.cmd --catalog "D:\MyRiff\catalog\enchor.jsonl" --artifact-root "D:\MyRiff\state\export-artifacts" --library-root "D:\MySongs"
Choose a different local settings location with --data-dir "D:\RiffLocalData".
Use --port 8950 to avoid a port already in use. Binding is always 127.0.0.1.
The launcher accepts --check for an offline import/runtime self-check.

WHAT WORKS / LIMITS
Browse and search your supplied catalog by title, artist, album and decade.
Vinyl rotation follows actual audio playback; swapping, pause, next, shuffle,
mute and keyboard controls work locally. Artwork needs an existing artwork-cache
folder adjacent to the supplied export-artifacts cache. Counts reflect only the
supplied checksum-valid catalog. No popularity estimates are invented.

Only validated cached packages can be previewed. No provider media is downloaded.
Audio codec support is checked when opened. Mixed-stem previews are capped at
45 seconds; full tracks and remote streaming are not promised.

Inventory reads .sng archives only, without unpacking or changing them. Unpacked
song folders are not indexed. Badges match artist/title metadata, not exact chart
variants. Linked Songs folders remain read-only.

The + control adds one explicitly confirmed cached .sng into an existing folder
inside your local .riff-companion/sandbox area. Existing files are never replaced.
Select the sandbox folder first, then confirm each chart. Folder extraction and
installation into the real Songs library are not part of this preview.

UNINSTALL / PRIVACY
Stop the terminal and delete the extracted distribution. If desired, separately
delete your .riff-companion settings/sandbox folder after checking its contents.
Your linked catalog, caches, and Songs folder are never deleted by this tool.
Never publish the local server or forward its port. It refuses non-loopback
clients/hostnames and foreign-origin mutations. Public Riff serves only this ZIP;
local filesystem APIs are not part of the public website.

LICENSES / INTEGRITY
LICENSE contains the project's existing rights notice. THIRD_PARTY_NOTICES.txt
and vendor/*dist-info/licenses preserve dependency notices; dependencies retain
their respective licenses. No new project license or music rights are granted.
RELEASE_MANIFEST.json lists every packaged file's SHA-256. Compare the downloaded
ZIP against its published SHA256SUMS.txt before use.
