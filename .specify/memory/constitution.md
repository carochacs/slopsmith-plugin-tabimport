# Tab Import — Constitution

## Inheritance

Slopsmith's core plugin contract governs everything in this repo (manifest,
plugin context: `get_dlc_dir`, `extract_meta`, `meta_db`, nav, asset serving).
This constitution lists Tab Import's own non-negotiables.

## Core Principles

### I. Use the Slopsmith CDLC builder, not a parallel one
Tab Import depends on core modules `gp2rs`, `gp2midi` (the bundled
`gp2midi.py` here is a thin local wrapper), `cdlc_builder`, and FluidSynth —
the same pipeline used elsewhere in the ecosystem. The plugin MUST NOT fork
its own SNG compiler or PSARC packer. If the core pipeline is missing, the
plugin must fail loudly with a clear error.

### II. Source format scope
Only Guitar Pro 3, 4, 5 (`.gp3`, `.gp4`, `.gp5`) are accepted. GP6 / GP7 use
a different binary format and are deliberately rejected. Adding support is a
`pyguitarpro` parser concern, not a plugin patch.

### III. WebSocket for progress, not polling
The build pipeline emits real-time progress via
`/ws/plugins/tab_import/build`. The client renders a stage description and
percentage. New progress channels MUST piggyback on this socket; do not add
HTTP polling endpoints.

### IV. Temp files: caller-supplied path round-trip
The upload step writes the GP file to a temp dir and returns its path; the
build step receives that path back. The client MUST NOT retain file contents
in JS after upload. The server is responsible for cleanup on success or
error. (See `analyze.md` for the known leak in some error paths.)

### V. Idempotent metadata cache update
On successful build, `_meta_db.put(filename, mtime, size, meta)` is called.
A throw here MUST NOT mark the build as failed — the PSARC was already
produced. Current code wraps the call in `try/except` and swallows errors.

### VI. Output filename safety
Filenames are derived from `title + "_" + artist` with `re.sub(r'[<>:"/\\|?*]', '_', …)`
plus a `_midi_p` suffix. Collisions overwrite. See open clarification on
collision handling.

## Governance

Amendments require corresponding updates to `routes.py` (build pipeline) and
`screen.js` (drop UI / progress wiring). Changes to the supported source
format set MUST update both `screen.js` (`ext` whitelist) and `routes.py`.

**Version**: 1.0.0 | **Ratified**: 2026-05-09 | **Last Amended**: 2026-05-09
