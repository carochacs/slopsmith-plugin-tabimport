# Clarifications — Tab Import

## Q1 — Why split upload and build into two endpoints (HTTP + WebSocket)?
**Resolved.** Upload is fast and cheap (parse only); build can take 30 s+ and
needs progress streaming. Splitting them lets the user review parsed
metadata and pick tracks before committing to a build, and avoids tying a
long-running socket to a probably-rejected GP file.

## Q2 — Why base64 over multipart upload?
**Resolved.** Simpler client (FileReader → JSON POST), no multipart parsing
on the server, and GP files are small enough that base64 overhead is
negligible. Multipart is a future option if file sizes grow.

## Q3 — Why is `gp2midi.py` shipped in this repo while `gp2rs` and
`cdlc_builder` are imported from the core?
**Resolved.** `gp2midi` here is a thin wrapper around the core's MIDI render
helpers. It's small enough to vendor and made changes on the plugin side
faster during initial development. Long-term it could move to core; today
moving it would be a low-priority refactor.

## Q4 — How are arrangements auto-selected?
**Resolved.** `auto_select_tracks(gp_path)` returns `(indices, name_map)`.
The mapping is heuristic by track name (`"bass" → Bass`, `"rhythm" →
Rhythm`, fallback `Lead`). User overrides are honoured: when the client
posts an explicit `tracks=…` list, the server rebuilds the name map from
track properties.

## Q5 — Filename collisions: clobber or append?
**Open.** Today: clobber. Adding `_2`, `_3` suffixes preserves history but
clutters the library. A "import again" flow that asks the user is also
possible. No decision recorded.

## Q6 — What's the temp-dir lifecycle?
**Resolved (mostly).** Upload uses `tempfile.mkdtemp()` and writes the GP
file. The build pipeline uses additional temp dirs for MIDI / XML output.
The build cleans up its own outputs on success but the original upload
temp dir is not explicitly removed in error paths. See `analyze.md`.

## Q7 — How is FluidSynth invoked?
**Resolved.** Through `gp2midi.gp_to_audio(gp_path, midi_out)`. FluidSynth
must be installed on the host with a soundfont path that the core knows
about. The Docker image bundles a soundfont; native installs may need
configuration.

## Q8 — What metadata does `_meta_db.put` store?
**Resolved.** Whatever `_extract_meta(out_path)` returns — title, artist,
album, arrangements, length, etc. The plugin doesn't peek into that struct;
it's a black-box hand-off to the meta DB.

## Q9 — Why a `(MIDI)` suffix on the CDLC title?
**Resolved.** Lets users tell at a glance that a song is MIDI-rendered (no
real audio). Keeps the listing honest when both versions exist side by side.
Open: should this be a per-import setting?
