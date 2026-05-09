# Tasks — Tab Import

Status legend: `DONE` (shipped in v1.0.0), `OPEN` (not yet implemented), `[P]` (parallelisable).

## US-1 — Drop GP file
- [DONE] Drop zone + click-to-browse with `<input type=file>` fallback.
- [DONE] Client-side extension whitelist (`gp3 / gp4 / gp5`).
- [DONE] base64 transit via `FileReader.readAsDataURL`.
- [DONE] Server-side extension recheck.

## US-2 — Auto-select arrangements
- [DONE] `auto_select_tracks(gp_path)` called and rendered.
- [DONE] Track checkboxes with arrangement label dropdowns.
- [DONE] Heuristic name map ("bass" / "rhythm" / fallback "Lead").

## US-3 — Edit metadata
- [DONE] Title / Artist / Album inputs pre-filled from GP file.
- [DONE] Server overrides GP metadata when client sends non-empty values.

## US-4 — Build with progress
- [DONE] WebSocket connection to `/ws/plugins/tab_import/build`.
- [DONE] Progress bar + stage label.
- [DONE] Build runs in `loop.run_in_executor(None, _do_build)`.
- [DONE] Progress queue with timeout polling.
- [DONE] Final `{done, filename, tracks}` message.

## US-5 — Error surfaces
- [DONE] Upload errors rendered inside the dropzone.
- [DONE] Build errors rendered with "Try again".
- [DONE] No DLC dir configured → clear error message.

## US-6 — Output naming
- [DONE] Sanitise forbidden chars.
- [DONE] `_midi_p.psarc` suffix.
- [DONE] `(MIDI)` suffix on title.
- [OPEN] [P] Collision strategy (Q5) — clobber today; option for `_2`/`_3`.

## Cross-cutting
- [DONE] `_meta_db.put` called on success (best-effort).
- [DONE] PSARC dropped into `get_dlc_dir()`.
- [OPEN] [P] Cleanup of upload `tmp_path` on abnormal WS disconnect (Q6).
- [OPEN] [P] Detect missing FluidSynth / soundfont at startup (Q7).
- [OPEN] Unit tests around `auto_select_tracks` heuristics on synthetic inputs.
- [OPEN] Multipart upload alternative for very large GP files (Q2).
