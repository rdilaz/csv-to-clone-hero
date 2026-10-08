# Riff Studio

Riff is a setlist builder: import music, find compatible charts, browse songs,
add them to a ZIP queue, then download verified playable files. The interface
puts selection and the queue ahead of match diagnostics.

## Whole-library selection

The prominent Add all control spans every result page. One chart per song is
the default; All chart versions is explicit. Imported music, additional artist
discoveries, or both can be selected together in one request. Choices outside
the requested scope are retained. Correct alternate recordings and review
matches require confirmation; blocked identities remain unavailable.

Actual selected songs and chart files are separate counts. Artist expansion
still uses the installed catalog and the explicit All eligible songs mode.
The studio redesign does not obtain a new provider catalog.

## Listening deck

Shuffle browses the eligible song queue, including pages not currently visible.
Next, Skip already added, Add to ZIP, and Undo last addition let users build
a setlist without repeatedly opening chart diagnostics. Add to ZIP changes
the selection, not the downloaded archive; ZIP preparation still validates
actual supported note tracks before publishing a file.

The preview is the original song through an official Spotify embed. It is
loaded only after an explicit preview action for a confirmed Spotify track ID.
Spotify determines playback availability. Artist discoveries without a
confirmed Spotify ID have no preview. Spotify audio is never extracted,
proxied, saved, or added to the chart ZIP. A chart for an alternate recording
may differ from the original Spotify song shown in the preview.

The decorative scene uses actual WebGL geometry with perspective and lighting.
Its movement is independent of music playback. Reduced motion, off-screen
visibility, and the motion control limit unnecessary graphics work. Chart
selection remains available if WebGL is unavailable.

## Performance and evidence

Work is batched by owner workspace and current discovery generation. Match
results are reused only while relevant library/candidate/validation state is
unchanged; selection overlays remain current. The UI sends a bulk request
instead of a request for every song. Versioned static text assets use bounded
gzip caching; account responses and chart archives are not handled by that
asset cache.

ZIP packaging samples at most three 64 KiB regions of each large artifact.
Incompressible packages are stored without another compression pass; small
or compressible packages retain deflate. Actual bytes and validation evidence
are preserved. Provider throttles, downloaded byte volume, and real note
inspection still determine total download time.

Local synthetic benchmarks and offline/browser checks accompany the review
package. They do not establish hosted account performance or owner ZIP
acceptance. Deploy the exact reviewed commit with the preserving host updater,
then verify the studio controls and a real ZIP on the hosting computer.

Official embed reference:
https://developer.spotify.com/documentation/embeds/tutorials/creating-an-embed
