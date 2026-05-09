# Plan — Tab Import (as built)

## File map

| File          | Lines | Purpose                                                                   |
|---------------|-------|---------------------------------------------------------------------------|
| `plugin.json` | 7     | Manifest. `id: tab_import`, version `1.0.0`, nav `Import Tab`, declares `screen.html`/`screen.js`/`routes.py`. |
| `routes.py`   | 205   | `POST /upload` (parse) + `WS /build` (progress-streaming pipeline).        |
| `screen.html` | 47    | Drop zone, parsed-metadata form, build-progress bar, result section.       |
| `screen.js`   | 177   | Drag-and-drop, base64 read, `WebSocket` progress consumer, UI state.       |
| `gp2midi.py`  | 206   | Thin wrapper around the core's MIDI render helpers (FluidSynth).           |

## Pipeline

```
[Browser]                                            [Server]
   drop GP file ──base64──► POST /upload ───────────► tempfile.mkdtemp()
                                                       guitarpro.parse()
                                                       auto_select_tracks()
   ◄── {title, artist, album, tracks[], tmp_path} ────
   user edits + Build click

   WS /build?tmp_path=…                              accept WS
   &title=…&artist=…&album=…&tracks=0,1
                                                    run executor:
                                                      gp2rs.convert_file()
                                                      gp2midi.gp_to_audio()
                                                      cdlc_builder.build_cdlc()
                                                      _meta_db.put()
   ◄── progress msgs (stage, progress %) every step
   ◄── {done: true, filename, tracks}
   close socket
```

## Endpoints

| Verb     | Path                                  | Purpose                                                       |
|----------|---------------------------------------|---------------------------------------------------------------|
| POST     | `/api/plugins/tab_import/upload`      | Parse GP file, return parsed metadata + `tmp_path`.            |
| WS       | `/ws/plugins/tab_import/build`        | Stream build progress; emit `{stage, progress}` then `{done}` or `{error}`. |

Query params on the WS: `tmp_path` (required), `title`, `artist`, `album`,
`tracks` (comma-separated indices).

## Plugin context dependencies

```python
_get_dlc_dir = context["get_dlc_dir"]
_extract_meta = context["extract_meta"]
_meta_db = context["meta_db"]
```

Imported from the core during the build:
- `gp2rs.convert_file`, `gp2rs.auto_select_tracks`
- `cdlc_builder.build_cdlc`
- `guitarpro` (pyguitarpro)
- `gp2midi.gp_to_audio` (this plugin's wrapper)

## Progress mapping

| Stage                                  | %      |
|----------------------------------------|--------|
| Parsing Guitar Pro file                | 10     |
| Auto-selecting tracks                  | 20     |
| Generating MIDI audio (FluidSynth)     | 30     |
| Converting to Rocksmith XML            | 50     |
| Compiling SNG / packing PSARC          | 60–95  |
| Updating meta DB / done                | 100    |

The 60–95% range is reported by `cdlc_builder.build_cdlc`'s `on_progress`
callback proxied through `report(msg, 60 + pct * 0.35)`.

## Error paths

- Unsupported extension → `{"error": "Unsupported format ..."}` synchronously
  on upload.
- Invalid base64 → `{"error": "Invalid file data"}`.
- Missing DLC dir on build → WS sends `{"error": "DLC folder not configured"}`.
- Missing temp file → `{"error": "File expired — please upload again"}`.
- Parse / synth / build raise → `_do_build` catches and emits
  `{"error": str(e)}` then closes.

## Risks / drift watchpoints

- **Core module drift**: `gp2rs`, `cdlc_builder` live in the Slopsmith core.
  Any rename or signature change breaks the build silently. Keep this plugin
  versioned alongside core changes.
- **Temp dir leak** (Q6): on abnormal disconnect mid-build the upload temp
  dir survives. Container restart cleans up; long-running native installs
  may accumulate.
- **FluidSynth soundfont** (Q7): non-Docker installs may have soundfont
  paths different from the bundled one.
- **Filename clobbering** (Q5): re-importing the same song overwrites.
