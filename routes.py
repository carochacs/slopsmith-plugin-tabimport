"""Tab Import plugin — drag and drop Guitar Pro files to create CDLC."""

import asyncio
import base64
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

_log = logging.getLogger("slopsmith.plugins.tab_import")

_get_dlc_dir = None
_extract_meta = None
_meta_db = None

# Routes are registered on a router at module level and included into the
# app in setup(). This avoids the NameError from using @app.post() at
# import time before setup() has been called.
router = APIRouter(prefix="/api/plugins/tab_import")

# /upload and /autosync write their working files under the system temp dir.
# The build endpoint receives those paths back from the client, so constrain
# them to the temp root before reading — and especially before deleting — so a
# crafted `tmp_path`/`audio_tmp_path` can't read arbitrary files or make the
# cleanup rmtree() escape the temp tree.
_TMP_ROOT = Path(tempfile.gettempdir()).resolve()
# Prefix for the per-upload session dirs this plugin creates, so paths handed
# back by the client can be recognised as plugin-owned before they're trusted.
_SESSION_PREFIX = "tabimport_"


def _under_tmp(p: str) -> bool:
    if not p:
        return False
    try:
        Path(p).resolve().relative_to(_TMP_ROOT)
        return True
    except (ValueError, OSError, RuntimeError):
        return False


def _valid_gp_session_path(p: str):
    """Resolve a client-supplied GP `tmp_path` to a trusted session file.

    /upload writes the GP file into a per-request mkdtemp() subdir. A path is
    only honoured if it's a regular file with a supported GP extension living
    in such a subdirectory — strictly inside the temp root, never the temp
    root itself. This rejects crafted paths that point at unrelated files or
    whose parent is the temp root (which /autosync would otherwise write the
    synced audio into). Returns the resolved Path, or None.
    """
    if not _under_tmp(p):
        return None
    rp = Path(p).resolve()
    if not rp.is_file() or rp.suffix.lower() not in _SUPPORTED_EXTENSIONS:
        return None
    # Must live in a plugin-owned session dir (created by /upload with this
    # prefix), strictly inside the temp root — not the temp root itself, and
    # not some unrelated temp file a client happened to point at.
    if rp.parent == _TMP_ROOT or not rp.parent.name.startswith(_SESSION_PREFIX):
        return None
    return rp

# Supported Guitar Pro file extensions.
# .gp3/.gp4/.gp5 are handled by PyGuitarPro via gp2rs.
# .gpx (Guitar Pro 6) is handled by gp2rs_gpx via the gp2rs shim (slopsmith#418/#616).
# .gp  (Guitar Pro 7/8) is supported for RS XML conversion via gp2rs_gpx.
#       Note: gp2midi still calls guitarpro.parse() so MIDI audio generation will
#       fail at the audio step — that is a separate follow-up.
_SUPPORTED_EXTENSIONS = {'.gp3', '.gp4', '.gp5', '.gpx', '.gp'}


def _build_sloppak(xml_paths, arrangement_names, audio_path, title, artist, album, output_path):
    """Pack arrangement XMLs + audio into a .sloppak zip (manifest.yaml + JSONs + stem)."""
    import json
    import xml.etree.ElementTree as ET
    import zipfile
    import yaml
    from lib.song import parse_arrangement, arrangement_to_wire

    work_dir = Path(tempfile.mkdtemp(prefix=_SESSION_PREFIX + "pack_"))
    try:
        arr_dir = work_dir / "arrangements"
        stems_dir = work_dir / "stems"
        arr_dir.mkdir()
        stems_dir.mkdir()

        audio_ext = Path(audio_path).suffix
        shutil.copy2(audio_path, stems_dir / f"full{audio_ext}")

        # Read songLength from first XML for the manifest duration field.
        duration = 0.0
        try:
            root0 = ET.parse(xml_paths[0]).getroot()
            sl = root0.get("songLength") or getattr(root0.find("songLength"), "text", None)
            if sl:
                duration = float(sl)
        except Exception:
            pass

        arr_entries = []
        used_ids: dict[str, int] = {}

        # Extract ebeats and sections from whichever XML has them — Vocals XMLs
        # carry no ebeats, so searching only idx==0 leaves beats empty when
        # Vocals is first. Once found, copy to every arrangement's wire.
        shared_ebeats: list | None = None
        shared_sections: list | None = None
        for _xml in xml_paths:
            try:
                _root = ET.parse(_xml).getroot()
                _eb = _root.find("ebeats")
                if _eb is not None:
                    _beats = [
                        {"time": float(e.get("time", 0)), "measure": int(e.get("measure", -1))}
                        for e in _eb.findall("ebeat")
                    ]
                    if _beats:
                        shared_ebeats = _beats
                        _sec = _root.find("sections")
                        if _sec is not None:
                            shared_sections = [
                                {
                                    "name": s.get("name", ""),
                                    "number": int(s.get("number", 0)),
                                    "startTime": float(s.get("startTime", 0)),
                                }
                                for s in _sec.findall("section")
                            ]
                        break
            except Exception:
                pass

        for idx, (xml_path, name) in enumerate(zip(xml_paths, arrangement_names)):
            arr = parse_arrangement(xml_path)
            wire = arrangement_to_wire(arr)

            # Inject shared ebeats/sections into every arrangement so the
            # metronome and highway have rhythmic reference on all tracks.
            if shared_ebeats:
                wire["ebeats"] = shared_ebeats
            if shared_sections:
                wire["sections"] = shared_sections

            raw_id = re.sub(r"[^a-z0-9]", "", name.lower()) or f"arr{idx}"
            count = used_ids.get(raw_id, 0)
            used_ids[raw_id] = count + 1
            arr_id = raw_id if count == 0 else f"{raw_id}{count + 1}"

            (arr_dir / f"{arr_id}.json").write_text(
                json.dumps(wire, ensure_ascii=False), encoding="utf-8"
            )

            tuning = list(arr.tuning) if hasattr(arr, "tuning") and arr.tuning else [0, 0, 0, 0, 0, 0]
            capo = arr.capo if hasattr(arr, "capo") else 0

            arr_entries.append({
                "id": arr_id,
                "name": name,
                "file": f"arrangements/{arr_id}.json",
                "tuning": tuning,
                "capo": capo,
            })

        manifest = {
            "title": title,
            "artist": artist,
            "album": album,
            "year": 0,
            "duration": duration,
            "stems": [{"id": "full", "file": f"stems/full{audio_ext}", "default": "on"}],
            "arrangements": arr_entries,
        }
        (work_dir / "manifest.yaml").write_text(
            yaml.dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )

        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(work_dir / "manifest.yaml", "manifest.yaml")
            for f in arr_dir.iterdir():
                zf.write(f, f"arrangements/{f.name}")
            for f in stems_dir.iterdir():
                zf.write(f, f"stems/{f.name}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def setup(app, context):
    global _get_dlc_dir, _extract_meta, _meta_db
    _get_dlc_dir = context["get_dlc_dir"]
    _extract_meta = context["extract_meta"]
    _meta_db = context["meta_db"]
    app.include_router(router)


@router.post("/upload")
async def upload_tab(data: dict):
    """Receive a GP file as base64, return parsed track info."""
    filename = data.get("filename", "")
    b64 = data.get("data", "")

    if not filename or not b64:
        return {"error": "No file data"}

    try:
        gp_data = base64.b64decode(b64, validate=True)
    except Exception:
        return {"error": "Invalid file data"}

    ext = Path(filename).suffix.lower()
    if ext not in _SUPPORTED_EXTENSIONS:
        supported = ", ".join(sorted(e.lstrip('.').upper() for e in _SUPPORTED_EXTENSIONS))
        return {"error": f"Unsupported format ({ext}). Supported formats: {supported}."}

    # Save to temp and parse. The session dir carries _SESSION_PREFIX so the
    # /autosync and /ws/build endpoints can recognise this path as plugin-owned.
    tmp = Path(tempfile.mkdtemp(prefix=_SESSION_PREFIX)) / Path(filename).name
    try:
        tmp.write_bytes(gp_data)
    except OSError:
        # disk full / permission — log detail server-side, clean up the new
        # session dir, return a generic message (no raw exception / paths).
        _log.exception("tab_import upload: failed to save uploaded file")
        shutil.rmtree(tmp.parent, ignore_errors=True)
        return {"error": "Could not save the uploaded file. See server logs for details."}

    try:
        # list_tracks and auto_select_tracks are both GPX-aware via the gp2rs shim
        # (gp2rs.list_tracks calls gp2rs_gpx.list_tracks for .gpx files transparently).
        from gp2rs import list_tracks, auto_select_tracks

        track_list = list_tracks(str(tmp))
        track_indices, name_map = auto_select_tracks(str(tmp))

        tracks = []
        for t in track_list:
            i = t['index']
            tracks.append({
                "index": i,
                "name": t['name'],
                "strings": t['strings'],
                "is_guitar": i in track_indices,
                "arrangement": name_map.get(i, ""),
                # Pass through the per-track flags the UI renders as badges /
                # note counts (with safe defaults so a missing key doesn't
                # surface as "undefined notes" or hide the badge).
                "is_drums": t.get('is_drums', False),
                "is_vocal": t.get('is_vocal', False),
                "is_piano": t.get('is_piano', False),
                "notes": t.get('notes', 0),
            })

        # Extract song metadata.
        # For GPX, read directly from the GPIF XML (guitarpro.parse fails on .gpx).
        # For GP3-5, use guitarpro.
        if ext in ('.gpx', '.gp'):
            from gp2rs_gpx import _load_gpif
            _root = _load_gpif(str(tmp))
            _score = _root.find('Score')
            _title = (_score.findtext('Title') or '').strip() if _score is not None else ''
            _artist = (_score.findtext('Artist') or '').strip() if _score is not None else ''
            _album = (_score.findtext('Album') or '').strip() if _score is not None else ''
        else:
            import guitarpro
            song = guitarpro.parse(str(tmp))
            _title = song.title or ''
            _artist = song.artist or ''
            _album = song.album or ''

        # Detect GP8 embedded audio
        has_audio = False
        sync_count = 0
        try:
            from gp8_audio_sync import has_embedded_audio, extract_sync
            has_audio = has_embedded_audio(str(tmp))
            if has_audio:
                sync = extract_sync(str(tmp))
                sync_count = len(sync.sync_points) if sync else 0
        except ImportError:
            # gp8_audio_sync not installed — embedded-audio detection is
            # optional, so treat as "no embedded audio".
            pass
        except Exception:
            # Detection is best-effort; don't fail the upload, but log so an
            # unexpected error in has_embedded_audio/extract_sync is debuggable.
            _log.debug("tab_import: embedded-audio detection failed", exc_info=True)

        return {
            "title": _title or Path(filename).stem,
            "artist": _artist or "Unknown",
            "album": _album or "",
            "tracks": tracks,
            "tmp_path": str(tmp),
            "has_embedded_audio": has_audio,
            "sync_point_count": sync_count,
        }

    except Exception:
        # Parse failed — tmp_path is only handed back on success, so drop the
        # session dir now instead of leaving it orphaned. Log detail
        # server-side; return a generic message (no raw exception / paths).
        _log.exception("tab_import upload: failed to parse %s", filename)
        shutil.rmtree(tmp.parent, ignore_errors=True)
        return {"error": "Could not parse this Guitar Pro file. See server logs for details."}




@router.post("/autosync")
async def tab_autosync(data: dict):
    """Auto-sync a GP file to a user-supplied audio file.

    Accepts the GP file's tmp_path and the audio as base64. Runs
    gp_autosync.auto_sync() and returns the GpSyncData as JSON so the
    frontend can display the result and the build endpoint can use it.

    Falls back gracefully if librosa is not installed — returns
    {"error": "..."} with a clear message so the frontend can continue
    with MIDI audio instead.
    """
    tmp_path = (data or {}).get("tmp_path", "")
    audio_b64 = (data or {}).get("audio_data", "")
    audio_filename = (data or {}).get("audio_filename", "audio.mp3")
    # JSON allows null / non-string here; coerce so Path(audio_filename) below
    # can't raise a TypeError and 500 the endpoint.
    if not isinstance(audio_filename, str) or not audio_filename:
        audio_filename = "audio.mp3"

    if not tmp_path or not audio_b64:
        return {"error": "tmp_path and audio_data required"}

    gp = _valid_gp_session_path(tmp_path)
    if gp is None:
        return {"error": "GP file expired — please re-upload"}

    # Write audio to temp file
    try:
        audio_bytes = base64.b64decode(audio_b64, validate=True)
    except Exception:
        return {"error": "Invalid audio data"}

    # Write the audio inside the uploaded GP file's own session dir (validated
    # above) under a fixed name with a validated extension. The build then
    # trusts it by session — it must live alongside the GP file — instead of
    # accepting an arbitrary temp path. Deriving the name also sidesteps any
    # path-traversal from audio_filename.
    _ext = Path(Path(audio_filename).name).suffix.lower()
    if _ext not in (".mp3", ".ogg", ".wav", ".flac", ".m4a", ".aac"):
        _ext = ".mp3"
    audio_tmp = gp.parent / f"synced_audio{_ext}"
    try:
        # Audio uploads can be many MB; write off the event loop so a large
        # base64 payload doesn't stall other requests.
        await asyncio.get_running_loop().run_in_executor(
            None, audio_tmp.write_bytes, audio_bytes
        )
    except OSError:
        # e.g. disk full / permission — log detail server-side, return a
        # generic message (no raw exception / paths) instead of a 500.
        _log.exception("tab_import autosync: failed to save audio file")
        audio_tmp.unlink(missing_ok=True)
        return {"error": "Could not save the audio file. See server logs for details."}

    # Only a successful sync hands the audio to the build. Every error path must
    # remove the file here (it lives in the GP dir, so unlink the file — never
    # the dir, which still holds the GP file).
    _keep_audio = False
    try:
        try:
            from gp_autosync import auto_sync, is_available
        except ImportError:
            return {"error": "Auto-sync is unavailable: the gp_autosync module (and its librosa dependency) isn't installed. Install the lyrics-karaoke plugin, or run: pip install librosa"}

        if not is_available():
            return {"error": "Auto-sync is unavailable: gp_autosync's librosa dependency isn't installed. Install the lyrics-karaoke plugin, or run: pip install librosa"}

        def on_progress(stage, pct):
            # /autosync is a one-shot HTTP call (no streaming channel); the
            # frontend just shows a generic "Auto-syncing..." status. Per-stage
            # progress is only surfaced later, over the build WebSocket.
            pass

        loop = asyncio.get_running_loop()
        sync = await loop.run_in_executor(
            None,
            lambda: auto_sync(str(gp), str(audio_tmp), progress_cb=on_progress)
        )

        _keep_audio = True  # success: build endpoint will consume + clean it
        return {
            "ok": True,
            "audio_offset": sync.audio_offset,
            "sync_point_count": len(sync.sync_points),
            "sync_points": [
                {
                    "bar": sp.bar,
                    "time_secs": round(sp.time_secs, 3),
                    "modified_bpm": round(sp.modified_tempo, 2),
                    "original_bpm": round(sp.original_tempo, 2),
                }
                for sp in sync.sync_points
            ],
            "audio_tmp_path": str(audio_tmp),
        }

    except Exception:
        # Log the detail server-side; return a generic message so internal
        # paths / library internals don't leak to the client.
        _log.exception("tab_import auto-sync failed")
        return {"error": "Auto-sync failed. See server logs for details."}
    finally:
        # Keep the file only on success (the build consumes it, and the GP
        # dir cleanup removes it). On error, unlink just the audio file — the
        # parent dir still holds the uploaded GP file.
        if not _keep_audio:
            audio_tmp.unlink(missing_ok=True)

_ALLOWED_ARRANGEMENTS = {"Lead", "Rhythm", "Bass", "Drums", "Keys", "Vocals"}


@router.websocket("/ws/build")
async def ws_build_tab(websocket: WebSocket, tmp_path: str, title: str = "",
                       artist: str = "", album: str = "", tracks: str = "",
                       arrangements: str = "", audio_mode: str = "midi",
                       audio_offset: float = 0.0, audio_tmp_path: str = "",
                       output_format: str = "sloppak"):
    """Build CDLC from an uploaded GP file with progress.

    audio_mode selects the backing track:
      - "embedded": extract the OGG embedded in a GP8 file (+ its sync offset)
      - "autosync": use the user audio aligned by /autosync (audio_tmp_path)
                    with the supplied audio_offset
      - "midi" (default/fallback): synthesise audio from the tab via gp2midi
    Real-audio modes skip MIDI synthesis (which can't parse .gpx/.gp anyway)
    and apply audio_offset so the chart lines up with the recording.
    """
    await websocket.accept()

    dlc = _get_dlc_dir()
    if not dlc:
        await websocket.send_json({"error": "DLC folder not configured"})
        await websocket.close()
        return

    gp = _valid_gp_session_path(tmp_path)
    if gp is None:
        await websocket.send_json({"error": "File expired — please upload again"})
        await websocket.close()
        return
    tmp_path = str(gp)  # use the validated, resolved session path from here on

    # Parse track indices
    try:
        track_indices = [int(x) for x in tracks.split(",") if x.strip()]
    except Exception:
        track_indices = []

    # Parse the user's per-track arrangement choices ("idx:Name" pairs); only
    # keep recognised arrangement names so the dropdown drives the build.
    user_arr: dict[int, str] = {}
    for _pair in arrangements.split(","):
        _idx, _, _name = _pair.partition(":")
        if _name in _ALLOWED_ARRANGEMENTS:
            try:
                user_arr[int(_idx)] = _name
            except ValueError:
                pass

    progress_queue = asyncio.Queue()

    # Temp dirs to remove once the build finishes (extracted embedded audio,
    # and the /autosync audio temp — which /autosync deliberately leaves on
    # disk for us to consume here, so the build owns its cleanup).
    _cleanup_dirs: list[str] = []

    def _register_cleanup(path: str) -> None:
        """Queue a per-request temp dir for removal after the build.

        Only ever a directory strictly *inside* the temp root — never the temp
        root itself — so a crafted path can't make the cleanup rmtree() escape
        the temp tree or wipe all temp files.
        """
        if not _under_tmp(path):
            return
        d = Path(path).resolve()
        # Only queue an actual directory strictly inside the temp root — never
        # the temp root itself, and never a file — so the rmtree() below can't
        # escape the temp tree or be pointed at a non-directory.
        if d != _TMP_ROOT and d.is_dir() and _under_tmp(str(d)):
            _cleanup_dirs.append(str(d))

    # The uploaded GP file lives in its own /upload mkdtemp() dir; remove it
    # once the build is done (it's otherwise never cleaned up). /autosync
    # writes the synced audio into this same dir, so this also reclaims it.
    _register_cleanup(str(Path(tmp_path).resolve().parent))

    def _do_build():
        def _emit(msg):
            # _do_build runs in an executor thread and asyncio.Queue is not
            # thread-safe, so marshal every queue write back onto the event
            # loop rather than calling put_nowait() across threads.
            loop.call_soon_threadsafe(progress_queue.put_nowait, msg)

        def report(stage, pct):
            _emit({"stage": stage, "progress": pct})

        try:
            gp_path = tmp_path
            report("Parsing Guitar Pro file...", 10)

            # convert_file and auto_select_tracks are GPX-aware via the gp2rs shim.
            # gp2midi.gp_to_audio still calls guitarpro.parse() internally, so MIDI
            # synthesis only works for GP3/4/5; for .gpx/.gp the user must supply
            # real audio (embedded or auto-synced), which the audio_mode branch
            # below routes around the MIDI step.
            from gp2rs import convert_file, auto_select_tracks, list_tracks
            from gp2midi import gp_to_audio

            if not track_indices:
                auto_indices, name_map = auto_select_tracks(gp_path)
            else:
                _, full_name_map = auto_select_tracks(gp_path)
                name_map = {i: full_name_map.get(i, 'Lead') for i in track_indices}
                auto_indices = track_indices

            if not auto_indices:
                _emit({"error": "No guitar/bass tracks found"})
                return

            # The user's explicit arrangement dropdown choices win over the
            # auto/heuristic assignment.
            for i in auto_indices:
                if i in user_arr:
                    name_map[i] = user_arr[i]

            arr_names = [name_map.get(i, "Lead") for i in auto_indices]
            report(f"Selected {len(auto_indices)} tracks: {', '.join(arr_names)}", 20)

            # Resolve the backing track. Real-audio modes (embedded/autosync)
            # skip MIDI synthesis and carry an audio_offset; on any failure we
            # fall back to MIDI so the build still completes.
            effective_mode = audio_mode
            build_audio_path = None
            effective_offset = 0.0

            if audio_mode == "embedded":
                try:
                    from gp8_audio_sync import extract_audio, extract_sync
                    report("Extracting embedded audio...", 30)
                    emb_dir = tempfile.mkdtemp()
                    _register_cleanup(emb_dir)
                    build_audio_path = extract_audio(gp_path, emb_dir)
                    _sync = extract_sync(gp_path)
                    effective_offset = _sync.audio_offset if _sync else 0.0
                    if not build_audio_path:
                        raise RuntimeError("no embedded OGG found")
                except Exception:
                    # Log detail server-side; the browser-facing progress line
                    # stays generic (no raw exception / paths).
                    _log.exception("tab_import build: embedded audio extraction failed")
                    report("Embedded audio unavailable; using MIDI...", 30)
                    effective_mode = "midi"
                    build_audio_path = None
            elif audio_mode == "autosync":
                # Trust the synced audio only if it's a regular file sitting in
                # the same session dir as the (already-validated) GP file —
                # not an arbitrary client-supplied temp path.
                _ap = Path(audio_tmp_path).resolve() if audio_tmp_path else None
                _gp_dir = Path(tmp_path).resolve().parent
                if _ap is not None and _ap.is_file() and _ap.parent == _gp_dir:
                    build_audio_path = str(_ap)
                    effective_offset = audio_offset
                else:
                    report("Synced audio expired; using MIDI...", 30)
                    effective_mode = "midi"

            if build_audio_path is None:
                effective_mode = "midi"
                # gp2midi synthesises via guitarpro.parse(), which can't read
                # GP6/7/8 — surface a clear, actionable error instead of letting
                # gp_to_audio() fail cryptically.
                if Path(gp_path).suffix.lower() in ('.gpx', '.gp'):
                    _emit({"error": "MIDI synthesis isn't supported for GP6/GP7/GP8 files. "
                                    "Use the embedded audio (GP8) or attach an audio file for auto-sync."})
                    return
                report("Generating MIDI audio...", 30)
                _midi_root = tempfile.mkdtemp()
                _register_cleanup(_midi_root)
                midi_out = os.path.join(_midi_root, "midi")
                build_audio_path = gp_to_audio(gp_path, midi_out)
                effective_offset = 0.0

            report("Converting to Rocksmith XML...", 50)
            xml_dir = tempfile.mkdtemp()
            _register_cleanup(xml_dir)
            xml_files = convert_file(gp_path, xml_dir,
                                     track_indices=auto_indices,
                                     audio_offset=effective_offset,
                                     arrangement_names=name_map)

            # Metadata: read the file's embedded title/artist/album, then let
            # any user-supplied field override it per-field. (Overriding only
            # one field must NOT blank the others.)
            _file_t = _file_a = _file_al = ''
            try:
                ext = Path(gp_path).suffix.lower()
                # .gpx (GP6) and .gp (GP7/8) both carry GPIF XML; only GP3/4/5
                # are read via guitarpro.parse() (which fails on .gpx/.gp).
                if ext in ('.gpx', '.gp'):
                    from gp2rs_gpx import _load_gpif
                    _root = _load_gpif(gp_path)
                    _score = _root.find('Score')
                    if _score is not None:
                        _file_t = (_score.findtext('Title') or '').strip()
                        _file_a = (_score.findtext('Artist') or '').strip()
                        _file_al = (_score.findtext('Album') or '').strip()
                else:
                    import guitarpro
                    song = guitarpro.parse(gp_path)
                    _file_t = song.title or ''
                    _file_a = song.artist or ''
                    _file_al = song.album or ''
            except Exception:
                pass  # fall back to user fields / stem below

            # Per-field precedence: user input → file metadata → safe default.
            t_str = (title or '').strip() or _file_t or Path(gp_path).stem
            a_str = (artist or '').strip() or _file_a or "Unknown"
            al_str = (album or '').strip() or _file_al

            # Tag the title/filename by audio source: "(MIDI)" only when the
            # backing track is synthesised, plain real-audio builds otherwise.
            _is_midi = effective_mode == "midi"
            _suffix = "_midi" if _is_midi else ""
            safe_t = re.sub(r'[<>:"/\\|?*]', '_', t_str)
            safe_a = re.sub(r'[<>:"/\\|?*]', '_', a_str)

            if output_format == "psarc":
                # Truncate to max 3 arrangements, keeping Lead/Rhythm/Bass priority
                from cdlc_builder import build_cdlc
                _priority = {"Lead": 0, "Rhythm": 1, "Bass": 2}
                paired = sorted(zip(arr_names, xml_files), key=lambda x: _priority.get(x[0], 99))
                paired = paired[:3]
                arr_names_out = [p[0] for p in paired]
                xml_files_out = [p[1] for p in paired]
                output = str(dlc / f"{safe_t}_{safe_a}{_suffix}_p.psarc")
                report("Building PSARC...", 60)
                build_cdlc(
                    xml_paths=xml_files_out,
                    arrangement_names=arr_names_out,
                    audio_path=build_audio_path,
                    title=f"{t_str} (MIDI)" if _is_midi else t_str,
                    artist=a_str,
                    album=al_str,
                    output_path=output,
                )
            else:
                output = str(dlc / f"{safe_t}_{safe_a}{_suffix}.sloppak")
                report("Packing sloppak...", 60)
                _build_sloppak(
                    xml_paths=xml_files,
                    arrangement_names=arr_names,
                    audio_path=build_audio_path,
                    title=f"{t_str} (MIDI)" if _is_midi else t_str,
                    artist=a_str,
                    album=al_str,
                    output_path=output,
                )

            # Cache metadata
            try:
                out_path = Path(output)
                meta = _extract_meta(out_path)
                stat = out_path.stat()
                _meta_db.put(out_path.name, stat.st_mtime, stat.st_size, meta)
            except Exception:
                # The PSARC is already built; a cache-write miss is non-fatal
                # (the scan repopulates it later), but log so it's visible.
                _log.debug("tab_import: metadata cache write failed", exc_info=True)

            _emit({
                "done": True, "progress": 100, "stage": "Complete!",
                "filename": Path(output).name,
                "tracks": ", ".join(arr_names),
                "audio_mode": effective_mode,
            })

        except Exception:
            # Detail goes to the server log; the client gets a generic message
            # so internal paths / library internals don't leak (same policy as
            # the /autosync handler).
            _log.exception("tab_import build failed")
            _emit({"error": "Build failed. See server logs for details."})

    loop = asyncio.get_running_loop()
    build_task = loop.run_in_executor(None, _do_build)

    try:
        while True:
            try:
                msg = await asyncio.wait_for(progress_queue.get(), timeout=1.0)
                await websocket.send_json(msg)
                if msg.get("done") or msg.get("error"):
                    break
            except asyncio.TimeoutError:
                if build_task.done():
                    # Flush any terminal message the worker queued just before
                    # returning, so a final error/done isn't dropped on the race
                    # between the put and this timeout.
                    while not progress_queue.empty():
                        await websocket.send_json(progress_queue.get_nowait())
                    break
    except WebSocketDisconnect:
        pass
    finally:
        # Make sure the worker has stopped touching the temp files, then remove
        # the consumed audio temp dirs (extracted embedded OGG / synced upload)
        # so they don't accumulate on disk (the /autosync endpoint leaves its
        # temp for the build to own and clean up here).
        try:
            await build_task
        except Exception:
            pass
        for _d in _cleanup_dirs:
            shutil.rmtree(_d, ignore_errors=True)

    await websocket.close()
