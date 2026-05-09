# Spec — Tab Import (`tab_import`)

> Retrospective spec for shipped v1.0.0. Implementation in `routes.py` (205
> lines), `screen.js` (177), `screen.html` (47), and `gp2midi.py` (206) is the
> source of truth.

## Summary

A Slopsmith plugin that lets users drag-and-drop Guitar Pro files (`.gp3` /
`.gp4` / `.gp5`) into the browser and produce a playable CDLC PSARC with
MIDI-rendered audio. The build runs entirely server-side; progress streams
via a WebSocket.

## User stories

### US-1 — Drag and drop a GP file
- **Given** I'm on the **Import Tab** screen,
  **When** I drop a `.gp3` / `.gp4` / `.gp5` file on the dropzone (or click
  to file-pick),
  **Then** the file is base64-encoded and POSTed to
  `/api/plugins/tab_import/upload`.
- The server returns parsed metadata (`title`, `artist`, `album`, `tracks[]`)
  and a `tmp_path` the client retains for the build step.

### US-2 — Auto-select arrangements
- **Given** the upload response,
  **When** the UI renders track checkboxes,
  **Then** Slopsmith's `auto_select_tracks(gp_path)` has pre-selected guitar
  / bass tracks and assigned default arrangement names ("Lead" / "Rhythm" /
  "Bass") via heuristics (track name keywords).
- The user can override selections and arrangement labels per track.

### US-3 — Edit metadata before build
- **Given** the parsed view is open,
  **When** I edit Title / Artist / Album,
  **Then** the values are sent in the build request and override the GP
  file's embedded metadata.

### US-4 — Build with live progress
- **Given** I click **Build CDLC**,
  **When** the client connects to
  `/ws/plugins/tab_import/build?tmp_path=...&title=...&...`,
  **Then** the server runs the pipeline in an executor thread and emits
  progress messages: `{stage, progress}` from 10% (parse) through 100%
  (complete).
- The pipeline:
  1. Parse GP via `pyguitarpro` (10%).
  2. Resolve track indices + arrangement names (20%).
  3. Render MIDI audio via FluidSynth (`gp2midi.gp_to_audio`) (30%).
  4. Convert to Rocksmith XML (`gp2rs.convert_file`) (50%).
  5. Build PSARC (`cdlc_builder.build_cdlc`) (60–95%).
  6. Update meta DB (`_meta_db.put`) — best-effort.
  7. Emit `{done: true, progress: 100, filename, tracks}`.

### US-5 — Surface errors clearly
- **Given** the upload step rejects an extension,
  **Then** the server returns `{error: "Unsupported format ..."}`.
- **Given** the build step fails (no DLC dir, no guitar/bass tracks, parse
  exception),
  **Then** the WebSocket emits `{error: "..."}` and the UI shows the message
  with a "Try again" affordance.

### US-6 — Output naming
- **Given** title `"Master of Puppets"` and artist `"Metallica"`,
  **Then** the output PSARC is
  `{DLC_DIR}/Master of Puppets_Metallica_midi_p.psarc`. Forbidden characters
  in either are replaced by underscore. The displayed CDLC title gets a
  `(MIDI)` suffix to distinguish from real-audio CDLC.

## Functional requirements

| ID    | Requirement                                                                                  | Source                  |
|-------|----------------------------------------------------------------------------------------------|-------------------------|
| FR-1  | `POST /api/plugins/tab_import/upload`: base64-decoded GP file → temp dir; return parsed metadata + `tmp_path`. | `routes.py`             |
| FR-2  | Reject unsupported extensions before writing temp file.                                      | `routes.py`             |
| FR-3  | `WebSocket /ws/plugins/tab_import/build`: stream `{stage, progress}` JSON messages; close on `done` or `error`. | `routes.py`             |
| FR-4  | Run the build in a thread executor; receive progress via `asyncio.Queue.put_nowait`.          | `routes.py`             |
| FR-5  | Drop UI accepts `.gp3 / .gp4 / .gp5` only (client-side check) and provides a hidden file input fallback. | `screen.js`             |
| FR-6  | Show parsed metadata + tracks before build; let user override Title / Artist / Album / track selection / arrangement label. | `screen.js`             |
| FR-7  | Sanitise filenames with `re.sub(r'[<>:"/\\|?*]', '_', …)`; suffix `_midi_p`.                  | `routes.py`             |
| FR-8  | Update `_meta_db` on success (best-effort, swallow failures).                                  | `routes.py`             |
| FR-9  | Suffix the CDLC `title` with `(MIDI)` to distinguish from real-audio CDLC.                    | `routes.py`             |
| FR-10 | Append the resulting PSARC to the user's DLC dir (`get_dlc_dir()`).                            | `routes.py`             |

## Non-functional

- **Latency**: a typical 3-minute song builds in <30 s on a developer laptop;
  most of the time is FluidSynth synthesis.
- **Memory**: the GP file is held entirely in memory during base64 transit;
  large GP files (>20 MB rare) could OOM the container.
- **Browser**: any modern browser with `FileReader`, `WebSocket`, drag-and-drop.

## Out of scope

- GP6 / GP7 support.
- Real audio (e.g. user-supplied MP3) instead of MIDI render — covered by
  the broader CDLC builder.
- Pitched output / re-tuning — pass-through from the GP file's tuning.
- Multi-file batch import — one file at a time.

## Open clarifications

- [NEEDS CLARIFICATION] Should colliding output filenames append a counter to
  avoid clobbering previous imports of the same song?
- [NEEDS CLARIFICATION] Should the `(MIDI)` suffix be configurable, e.g. for
  users intentionally re-rendering a CDLC?
- [NEEDS CLARIFICATION] What is the expected behaviour when FluidSynth or
  pyguitarpro is missing from the host? Today the `_do_build` thread raises
  inside the executor and surfaces a generic error.
- [NEEDS CLARIFICATION] What is the cleanup story for `tmp_path` on
  abnormal disconnects?
