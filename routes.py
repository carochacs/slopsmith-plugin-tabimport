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


def _extract_lyrics_gp5(gp_path: str, track_idx: int,
                         audio_offset: float = 0.0) -> list | None:
    """Extract timestamped lyrics from a GP3/4/5 vocal track.

    Tries two sources in order:
    1. beat.text — syllable annotations placed manually on each beat.
    2. song.lyrics — Guitar Pro's dedicated lyrics editor, which stores a
       block of text keyed to a track and a start measure.  Most GP vocal
       tracks use this rather than beat.text.

    Returns a list of {"t": float, "d": float, "w": str} dicts, or None.
    Only works for GP3/4/5 — guitarpro.parse() rejects .gpx/.gp.
    """
    try:
        import guitarpro
        song = guitarpro.parse(gp_path)
        if track_idx < 0 or track_idx >= len(song.tracks):
            return None
        track = song.tracks[track_idx]

        # ── Build a beat-time map once (shared by both extraction paths) ──
        # beat_times[measure_idx][beat_idx] = (start_secs, duration_secs)
        bpm = float(song.tempo)
        beat_times: list[list[tuple[float, float]]] = []
        current_time = 0.0
        for header, measure in zip(song.measureHeaders, track.measures):
            bpm = float(header.tempo.value)
            measure_beats = []
            for beat in measure.voices[0].beats:
                dur = beat.duration
                qn = 4.0 / dur.value
                if dur.isDotted:
                    qn *= 1.5
                elif getattr(dur, 'isDoubleDotted', False):
                    qn *= 1.75
                tup = dur.tuplet
                if tup.enters != tup.times:
                    qn *= tup.times / tup.enters
                beat_secs = qn * 60.0 / bpm
                mtc = getattr(getattr(beat, 'effect', None), 'mixTableChange', None)
                if mtc and getattr(getattr(mtc, 'tempo', None), 'value', 0) > 0:
                    bpm = float(mtc.tempo.value)
                measure_beats.append((current_time, beat_secs))
                current_time += beat_secs
            beat_times.append(measure_beats)

        # ── Path 1: beat.text annotations ──
        words: list[dict] = []
        for mi, (header, measure) in enumerate(zip(song.measureHeaders, track.measures)):
            for bi, beat in enumerate(measure.voices[0].beats):
                text = getattr(getattr(beat, 'text', None), 'value', '') or ''
                if text and mi < len(beat_times) and bi < len(beat_times[mi]):
                    t, d = beat_times[mi][bi]
                    words.append({
                        "t": round(t + audio_offset, 3),
                        "d": round(d, 3),
                        "w": text,
                    })
        if words:
            return words

        # ── Path 2: song.lyrics (Guitar Pro dedicated lyrics editor) ──
        lyr = getattr(song, 'lyrics', None)
        if lyr is None:
            return None
        # trackChoice is 1-based in pyguitarpro; track_idx is 0-based.
        if getattr(lyr, 'trackChoice', -1) - 1 != track_idx:
            return None
        lines = getattr(lyr, 'lines', None) or []
        for line_text, start_measure in lines:
            if not line_text:
                continue
            syllables = line_text.split()
            # start_measure is 1-based measure number
            mi = max(0, start_measure - 1)
            syl_idx = 0
            while syl_idx < len(syllables) and mi < len(beat_times):
                for bi, (t, d) in enumerate(beat_times[mi]):
                    if syl_idx >= len(syllables):
                        break
                    words.append({
                        "t": round(t + audio_offset, 3),
                        "d": round(d, 3),
                        "w": syllables[syl_idx],
                    })
                    syl_idx += 1
                mi += 1

        return words if words else None
    except Exception:
        _log.debug("tab_import: lyrics extraction failed", exc_info=True)
        return None


def _extract_sections_gp5(gp_path: str, audio_offset: float = 0.0) -> list | None:
    """Extract section markers from a GP3/4/5 file via measure-header markers."""
    try:
        import guitarpro
        song = guitarpro.parse(gp_path)
        bpm = float(song.tempo)
        current_time = 0.0
        sections = []
        for header in song.measureHeaders:
            bpm = float(header.tempo.value)
            marker = getattr(header, 'marker', None)
            if marker and getattr(marker, 'title', ''):
                sections.append({
                    "name": marker.title,
                    "number": len(sections) + 1,
                    "time": round(current_time + audio_offset, 3),
                })
            ts = header.timeSignature
            beats = ts.numerator * (4.0 / ts.denominator)
            current_time += beats * 60.0 / bpm
        return sections or None
    except Exception:
        _log.debug("tab_import: GP5 section extraction failed", exc_info=True)
        return None


def _extract_sections_gpif(gp_path: str, audio_offset: float = 0.0) -> list | None:
    """Extract section markers from a GP6/GP7/GP8 file via GPIF XML MasterBars."""
    try:
        from gp2rs_gpx import _load_gpif
        root = _load_gpif(gp_path)
        sections = []
        current_time = 0.0
        bpm = 120.0
        for mb in root.findall('MasterBars/MasterBar'):
            tempo_val = mb.findtext('Tempo/Value')
            if tempo_val:
                bpm = float(tempo_val)
            sec_el = mb.find('Section')
            if sec_el is not None:
                text = (sec_el.findtext('Text') or sec_el.get('text', '')).strip()
                if text:
                    sections.append({
                        "name": text,
                        "number": len(sections) + 1,
                        "time": round(current_time + audio_offset, 3),
                    })
            ts_text = mb.findtext('Time') or ''
            if '/' in ts_text:
                num, den = ts_text.split('/', 1)
                beats = int(num.strip()) * (4.0 / int(den.strip()))
            else:
                beats = 4.0
            current_time += beats * 60.0 / bpm
        return sections or None
    except Exception:
        _log.debug("tab_import: GPIF section extraction failed", exc_info=True)
        return None


# General MIDI program → human-readable name for the programs GuitarPro uses
# on electric guitar tracks. 0-indexed (GP stores 0-127).
_GP_PROGRAM_NAMES: dict[int, str] = {
    24: "Nylon Guitar",
    25: "Steel Guitar",
    26: "Jazz Guitar",
    27: "Clean Guitar",
    28: "Muted Guitar",
    29: "Overdrive",
    30: "Distortion",
    31: "Harmonics",
}


def _extract_tones_gp5(gp_path: str, track_idx: int,
                        audio_offset: float = 0.0) -> dict | None:
    """Extract tone changes from MixTableChange.instrument events in a GP3/4/5 file.

    Returns a tones dict with the same shape as Arrangement.tones:
      {"base": str, "changes": [{"t": float, "name": str}], "definitions": []}
    or None if no instrument-change events are found.
    """
    try:
        import guitarpro
        song = guitarpro.parse(gp_path)
        if track_idx < 0 or track_idx >= len(song.tracks):
            return None
        track = song.tracks[track_idx]

        # Starting instrument from the track channel
        current_program = getattr(getattr(track, 'channel', None), 'instrument', None)
        if current_program is None:
            return None

        def _name(prog: int) -> str:
            return _GP_PROGRAM_NAMES.get(prog, f"Tone {prog + 1}")

        base_name = _name(current_program)
        changes: list[dict] = []

        bpm = float(song.tempo)
        current_time = 0.0

        for header, measure in zip(song.measureHeaders, track.measures):
            bpm = float(header.tempo.value)
            for beat in measure.voices[0].beats:
                mtc = getattr(getattr(beat, 'effect', None), 'mixTableChange', None)
                if mtc is not None:
                    instr = getattr(mtc, 'instrument', None)
                    if instr is not None:
                        new_prog = getattr(instr, 'value', None)
                        if new_prog is not None and new_prog != current_program:
                            changes.append({
                                "t": round(current_time + audio_offset, 3),
                                "name": _name(new_prog),
                            })
                            current_program = new_prog

                # Update tempo if this beat carries a tempo MixTableChange
                if mtc is not None and getattr(getattr(mtc, 'tempo', None), 'value', 0) > 0:
                    bpm = float(mtc.tempo.value)

                # Advance time by beat duration
                dur = beat.duration
                qn = 4.0 / dur.value
                if dur.isDotted:
                    qn *= 1.5
                elif getattr(dur, 'isDoubleDotted', False):
                    qn *= 1.75
                tup = dur.tuplet
                if tup.enters != tup.times:
                    qn *= tup.times / tup.enters
                current_time += qn * 60.0 / bpm

        if not changes:
            return None
        return {"base": base_name, "changes": changes, "definitions": []}
    except Exception:
        _log.debug("tab_import: GP5 tone extraction failed", exc_info=True)
        return None


def _merge_rs_xmls(primary_xml: str, secondary_xmls: list) -> str:
    """Merge note-bearing elements from secondary RS XMLs into the primary XML.

    Combines <notes>, <chords>, and <handShapes> child elements from each
    secondary into the primary and re-sorts them by time.  The primary file is
    overwritten in place; its path is returned for convenience.
    """
    import xml.etree.ElementTree as ET

    tree = ET.parse(primary_xml)
    root = tree.getroot()

    for sec_xml in secondary_xmls:
        try:
            sec_root = ET.parse(sec_xml).getroot()
            for tag in ('notes', 'chords', 'handShapes'):
                sec_cont = sec_root.find(tag)
                if sec_cont is None or len(sec_cont) == 0:
                    continue
                pri_cont = root.find(tag)
                if pri_cont is None:
                    root.append(sec_cont)
                else:
                    for child in list(sec_cont):
                        pri_cont.append(child)
        except Exception:
            _log.debug("tab_import: XML merge skipped for %s", sec_xml, exc_info=True)

    for tag in ('notes', 'chords', 'handShapes'):
        cont = root.find(tag)
        if cont is not None and len(cont) > 0:
            cont[:] = sorted(cont, key=lambda e: float(e.get('time', 0)))

    tree.write(primary_xml, encoding='unicode', xml_declaration=False)
    return primary_xml


def _extract_lyrics_gpif(gp_path: str, vocals_track_idx: int,
                          audio_offset: float = 0.0) -> list | None:
    """Extract timestamped lyrics from a GP6/GP7/GP8 vocal track via GPIF XML.

    Mirrors _extract_lyrics_gp5 but works on the GPIF beat graph instead of
    pyguitarpro objects. vocals_track_idx is the 1-based gp2rs track index.
    Returns {"t", "d", "w"} dicts or None.
    """
    try:
        from gp2rs_gpx import _load_gpif
        root = _load_gpif(gp_path)

        # Beat pool: id → {duration_value, text}
        beats_pool: dict[str, dict] = {}
        for b in root.findall('Beats/Beat'):
            bid = b.get('id')
            if bid is None:
                continue
            beats_pool[bid] = {
                'dur': float(b.findtext('Duration') or 4),
                'text': (b.findtext('FreeText') or '').strip(),
            }

        # Voice pool: id → ordered beat-id list
        voices_pool: dict[str, list[str]] = {}
        for v in root.findall('Voices/Voice'):
            vid = v.get('id')
            if vid is None:
                continue
            voices_pool[vid] = [
                bref.get('ref') for bref in v.findall('Beats/Beat')
                if bref.get('ref')
            ]

        # Bar pool: id → first-voice-id
        bars_pool: dict[str, str] = {}
        for bar in root.findall('Bars/Bar'):
            bid = bar.get('id')
            vref = bar.find('Voices/Voice')
            if bid and vref is not None and vref.get('ref'):
                bars_pool[bid] = vref.get('ref')

        # Walk MasterBars in order; select the bar for this track (0-based).
        track_bar_idx = vocals_track_idx - 1  # gp2rs is 1-based
        words: list[dict] = []
        current_time = 0.0
        bpm = 120.0

        for mb in root.findall('MasterBars/MasterBar'):
            tempo_val = mb.findtext('Tempo/Value')
            if tempo_val:
                bpm = float(tempo_val)

            bar_refs = mb.findall('Bars/Bar')
            if track_bar_idx >= len(bar_refs):
                continue
            bar_ref = bar_refs[track_bar_idx].get('ref')
            if not bar_ref or bar_ref not in bars_pool:
                continue
            voice_id = bars_pool[bar_ref]
            beat_ids = voices_pool.get(voice_id, [])

            for bid in beat_ids:
                beat = beats_pool.get(bid)
                if beat is None:
                    continue
                # GPIF Duration: 4 = quarter note (same denominator convention as guitarpro)
                qn = 4.0 / beat['dur']
                beat_secs = qn * 60.0 / bpm
                if beat['text']:
                    words.append({
                        "t": round(current_time + audio_offset, 3),
                        "d": round(beat_secs, 3),
                        "w": beat['text'],
                    })
                current_time += beat_secs

        return words if words else None
    except Exception:
        _log.debug("tab_import: GPIF lyrics extraction failed", exc_info=True)
        return None



def _build_sloppak(xml_paths, arrangement_names, audio_path, title, artist, album,
                   output_path, lyrics=None, extra_sections=None, cover_path=None,
                   tones_data=None):
    """Pack arrangement XMLs + audio into a .sloppak zip (manifest.yaml + JSONs + stem).

    extra_sections: fallback sections list (from GP markers) used when the
    Rocksmith XMLs carry no <sections> element of their own.
    """
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
                                    # sloppak loader reads "time"; RS XML uses
                                    # the camelCase "startTime" attribute here
                                    "time": float(s.get("startTime", 0)),
                                }
                                for s in _sec.findall("section")
                            ]
                        break
            except Exception:
                pass

        # Fall back to GP-marker sections when the XML carries none.
        if not shared_sections and extra_sections:
            shared_sections = extra_sections

        # Last resort: synthesise ~10 evenly-spaced sections from ebeat measure
        # boundaries so navigation works even when the GP file has no markers.
        if not shared_sections and shared_ebeats:
            ms = [e for e in shared_ebeats if e.get('measure', -1) > 0]
            step = max(1, len(ms) // 10)
            shared_sections = [
                {"name": f"Section {i + 1}", "number": i + 1,
                 "time": ms[j]['time']}
                for i, j in enumerate(range(0, len(ms), step))
            ]

        # Pair XMLs with names; if gp2rs produced fewer files than expected
        # (it silently skips unsupported tracks) truncate names to match.
        if len(xml_paths) != len(arrangement_names):
            _log.warning(
                "tab_import: xml count %d != arrangement count %d; truncating",
                len(xml_paths), len(arrangement_names))
            arrangement_names = arrangement_names[:len(xml_paths)]

        for idx, (xml_path, name) in enumerate(zip(xml_paths, arrangement_names)):
            arr = parse_arrangement(xml_path)
            wire = arrangement_to_wire(arr)

            # Inject shared ebeats/sections into every arrangement so the
            # metronome and highway have rhythmic reference on all tracks.
            if shared_ebeats:
                wire["ebeats"] = shared_ebeats
            if shared_sections:
                wire["sections"] = shared_sections

            # Inject MIDI-program-based tone data (from MixTableChange.instrument)
            # when available and the arrangement carries no tone definitions.
            if not arr.tones and tones_data:
                wire["tones"] = tones_data


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
        if lyrics is not None:
            (work_dir / "lyrics.json").write_text(
                json.dumps(lyrics, ensure_ascii=False), encoding="utf-8"
            )
            manifest["lyrics"] = "lyrics.json"
            manifest["lyrics_source"] = "gp"

        if cover_path:
            _cp = Path(cover_path)
            if _cp.is_file():
                manifest["cover"] = "cover" + _cp.suffix.lower()

        (work_dir / "manifest.yaml").write_text(
            yaml.dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )

        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(work_dir / "manifest.yaml", "manifest.yaml")
            for f in arr_dir.iterdir():
                zf.write(f, f"arrangements/{f.name}")
            for f in stems_dir.iterdir():
                zf.write(f, f"stems/{f.name}")
            if lyrics is not None:
                zf.write(work_dir / "lyrics.json", "lyrics.json")
            if cover_path:
                _cp = Path(cover_path)
                if _cp.is_file():
                    zf.write(_cp, "cover" + _cp.suffix.lower())
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
            # GP6/GP7/GP8 can't use MIDI synthesis (guitarpro.parse fails on
            # .gpx/.gp). Flag this so the frontend can require audio before
            # allowing the build.
            "requires_audio": Path(filename).suffix.lower() in ('.gpx', '.gp'),
        }

    except Exception:
        # Parse failed — tmp_path is only handed back on success, so drop the
        # session dir now instead of leaving it orphaned. Log detail
        # server-side; return a generic message (no raw exception / paths).
        _log.exception("tab_import upload: failed to parse %s", filename)
        shutil.rmtree(tmp.parent, ignore_errors=True)
        return {"error": "Could not parse this Guitar Pro file. See server logs for details."}




@router.post("/youtube-audio")
async def youtube_audio(data: dict):
    """Download audio from a YouTube (or yt-dlp-supported) URL.

    Returns a server-side tmp_path the build WebSocket can consume directly,
    avoiding a second upload round-trip through /autosync.
    """
    url = (data or {}).get("url", "").strip()
    if not url:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "No URL provided"}, status_code=400)

    # Validate the GP session dir so we can drop the audio there — the build
    # endpoint requires audio_tmp_path to share a parent with the GP file.
    gp_tmp_path = (data or {}).get("gp_tmp_path", "")
    gp_session_dir: Path | None = None
    if gp_tmp_path:
        gp_rp = _valid_gp_session_path(gp_tmp_path)
        if gp_rp:
            gp_session_dir = gp_rp.parent

    start_time = data.get("start_time")
    end_time = data.get("end_time")
    try:
        start_time = float(start_time) if start_time is not None else None
    except (ValueError, TypeError):
        start_time = None
    try:
        end_time = float(end_time) if end_time is not None else None
    except (ValueError, TypeError):
        end_time = None
    if start_time is not None and end_time is not None and end_time <= start_time:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "end_time must be greater than start_time"}, status_code=400)

    def _download():
        import subprocess
        # Use a private temp dir for the yt-dlp download step; final audio
        # is moved into the GP session dir so the build endpoint accepts it.
        tmp = Path(tempfile.mkdtemp(prefix=_SESSION_PREFIX))
        out_template = str(tmp / "audio.%(ext)s")
        try:
            import yt_dlp
            opts = {
                "format": "bestaudio/best",
                "outtmpl": out_template,
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "vorbis",
                    "preferredquality": "5",
                }],
                "quiet": True,
                "no_warnings": True,
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                title = info.get("title", "audio")

            downloaded = next(
                (f for f in tmp.iterdir() if f.suffix in (".ogg", ".mp3", ".m4a", ".wav")),
                None,
            )
            if downloaded is None:
                raise RuntimeError("No audio file produced by yt-dlp")

            if start_time is not None or end_time is not None:
                trimmed = tmp / ("trimmed" + downloaded.suffix)
                cmd = ["ffmpeg", "-y", "-i", str(downloaded)]
                if start_time is not None:
                    cmd.extend(["-ss", str(start_time)])
                if end_time is not None:
                    dur = end_time - (start_time or 0)
                    cmd.extend(["-t", str(dur)])
                cmd.append(str(trimmed))
                subprocess.run(cmd, check=True, capture_output=True)
                downloaded.unlink(missing_ok=True)
                downloaded = trimmed

            # Move into the GP session dir so the build endpoint's path
            # check (audio must share parent with the GP file) passes.
            dest_dir = gp_session_dir if gp_session_dir else tmp
            dest = dest_dir / ("yt_audio" + downloaded.suffix)
            shutil.move(str(downloaded), dest)
            shutil.rmtree(tmp, ignore_errors=True)

            return {"tmp_path": str(dest), "title": title}
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

    try:
        result = await asyncio.get_event_loop().run_in_executor(None, _download)
        return result
    except ImportError:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "yt-dlp is not installed on this server"}, status_code=500)
    except Exception as e:
        _log.exception("tab_import youtube-audio: download failed for %r", url)
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/upload-cover")
async def upload_cover(data: dict):
    """Save a base64-encoded cover image into the GP session dir.

    Returns cover_path (server-side) that the build WebSocket can consume.
    """
    gp_tmp_path = (data or {}).get("gp_tmp_path", "")
    b64 = (data or {}).get("data", "")
    filename = (data or {}).get("filename", "cover.jpg")

    if not gp_tmp_path or not b64:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "Missing gp_tmp_path or data"}, status_code=400)

    gp_rp = _valid_gp_session_path(gp_tmp_path)
    if not gp_rp:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "Invalid gp_tmp_path"}, status_code=400)

    ext = Path(filename).suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"

    try:
        img_bytes = base64.b64decode(b64, validate=True)
    except Exception:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "Invalid image data"}, status_code=400)

    dest = gp_rp.parent / f"cover{ext}"
    dest.write_bytes(img_bytes)
    return {"cover_path": str(dest)}


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
    # _keep_audio gates the finally-block cleanup: True means the build
    # endpoint owns the file and will remove it via the session-dir rmtree.
    _keep_audio = False
    try:
        try:
            from gp_autosync import auto_sync, is_available
        except ImportError:
            # gp_autosync / librosa not installed. Keep the audio and return it
            # at offset 0 so the build uses real audio without time alignment,
            # rather than silently discarding the file and falling back to MIDI.
            _keep_audio = True
            return {
                "audio_tmp_path": str(audio_tmp),
                "audio_offset": 0.0,
                "sync_point_count": 0,
                "sync_points": [],
                "sync_skipped": "Auto-sync not available (gp_autosync/librosa not installed); audio will play without time alignment.",
            }

        if not is_available():
            _keep_audio = True
            return {
                "audio_tmp_path": str(audio_tmp),
                "audio_offset": 0.0,
                "sync_point_count": 0,
                "sync_points": [],
                "sync_skipped": "Auto-sync not available (gp_autosync/librosa not installed); audio will play without time alignment.",
            }

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
        # Sync algorithm failed at runtime. Log detail server-side. Keep the
        # audio and return it at offset 0 — same degraded-success contract as
        # the missing-library paths above — so the build uses real audio rather
        # than discarding the file and falling back to MIDI synthesis.
        _log.exception("tab_import auto-sync failed; keeping audio at offset 0")
        _keep_audio = True
        return {
            "audio_tmp_path": str(audio_tmp),
            "audio_offset": 0.0,
            "sync_point_count": 0,
            "sync_points": [],
            "sync_skipped": "Auto-sync failed; audio will play without time alignment.",
        }
    finally:
        # Keep the file only when the build will consume it (and remove it via
        # the session-dir rmtree). On disk-write errors the file was never
        # created, so unlink is a no-op.
        if not _keep_audio:
            audio_tmp.unlink(missing_ok=True)

_ALLOWED_ARRANGEMENTS = {"Lead", "Rhythm", "Bass", "Drums", "Keys", "Vocals"}


@router.websocket("/ws/build")
async def ws_build_tab(websocket: WebSocket, tmp_path: str, title: str = "",
                       artist: str = "", album: str = "", tracks: str = "",
                       arrangements: str = "", audio_mode: str = "midi",
                       audio_offset: float = 0.0, audio_tmp_path: str = "",
                       combine: int = 0, cover_path: str = ""):
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

            # Separate vocals tracks BEFORE XML conversion — convert_file
            # silently drops vocals for .gp/.gpx files, which would corrupt the
            # zip(xml_files, arr_names) alignment if we passed them through.
            vocal_auto_indices = [ai for ai, n in zip(auto_indices, arr_names) if n == "Vocals"]
            non_vocal_auto = [ai for ai, n in zip(auto_indices, arr_names) if n != "Vocals"]
            non_vocal_arr_names = [n for n in arr_names if n != "Vocals"]

            # If the user selected ONLY vocals, keep the track in arrangements
            # as a fallback so the build still produces something playable.
            if not non_vocal_auto:
                non_vocal_auto = auto_indices
                non_vocal_arr_names = arr_names

            report("Converting to Rocksmith XML...", 50)
            xml_dir = tempfile.mkdtemp()
            _register_cleanup(xml_dir)
            # Pass full name_map so gp2rs can resolve arrangement names for any
            # track index it knows about; only non_vocal_auto are converted.
            xml_files = convert_file(gp_path, xml_dir,
                                     track_indices=non_vocal_auto,
                                     audio_offset=effective_offset,
                                     arrangement_names=name_map)
            arr_names = non_vocal_arr_names

            # Combine same-name arrangements when requested.  Track indices
            # with the same arrangement name are merged into one XML so that
            # Songsterr-style tone-change tracks become a single playable part.
            if combine:
                from collections import OrderedDict
                groups: dict[str, list] = OrderedDict()
                for xml_f, nm in zip(xml_files, arr_names):
                    groups.setdefault(nm, []).append(xml_f)
                merged_xmls, merged_names = [], []
                for nm, files in groups.items():
                    if len(files) > 1:
                        _merge_rs_xmls(files[0], files[1:])
                    merged_xmls.append(files[0])
                    merged_names.append(nm)
                xml_files = merged_xmls
                arr_names = merged_names

            # Extract timestamped lyrics from the first vocals track.
            # GP3/4/5: use pyguitarpro (0-based index = 1-based gp2rs idx - 1).
            # GP6/7/8 (.gpx/.gp): use the GPIF XML beat graph parser.
            lyrics_data = None
            if vocal_auto_indices:
                ext = Path(gp_path).suffix.lower()
                report("Extracting lyrics...", 55)
                if ext in ('.gpx', '.gp'):
                    lyrics_data = _extract_lyrics_gpif(gp_path, vocal_auto_indices[0],
                                                       effective_offset)
                else:
                    lyrics_data = _extract_lyrics_gp5(gp_path, vocal_auto_indices[0] - 1,
                                                      effective_offset)

            # Extract GP section markers to inject into the sloppak when gp2rs
            # produces no <sections> element (common for Songsterr exports).
            gp_sections = None
            _ext = Path(gp_path).suffix.lower()
            if _ext in ('.gpx', '.gp'):
                gp_sections = _extract_sections_gpif(gp_path, effective_offset)
            else:
                gp_sections = _extract_sections_gp5(gp_path, effective_offset)

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

            output = str(dlc / f"{safe_t}_{safe_a}{_suffix}.sloppak")
            report("Packing sloppak...", 60)

            # Validate cover_path: must be a regular file in the same session
            # dir as the GP file, with an allowed image extension.
            _cover_path = None
            if cover_path:
                _cvp = Path(cover_path).resolve()
                _gp_dir = Path(gp_path).resolve().parent
                if (_cvp.is_file() and _cvp.parent == _gp_dir
                        and _cvp.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")):
                    _cover_path = str(_cvp)

            # Extract overdrive/instrument tone data from MixTableChange for
            # GP3/4/5 files.  Use the first selected track as the reference.
            _tones_data = None
            if Path(gp_path).suffix.lower() not in ('.gpx', '.gp') and auto_indices:
                _primary_track_idx = auto_indices[0] - 1  # gp2rs is 1-based
                _tones_data = _extract_tones_gp5(gp_path, _primary_track_idx, effective_offset)

            _build_sloppak(
                xml_paths=xml_files,
                arrangement_names=arr_names,
                audio_path=build_audio_path,
                title=f"{t_str} (MIDI)" if _is_midi else t_str,
                artist=a_str,
                album=al_str,
                output_path=output,
                lyrics=lyrics_data,
                extra_sections=gp_sections,
                cover_path=_cover_path,
                tones_data=_tones_data,
            )

            # Cache metadata
            try:
                out_path = Path(output)
                meta = _extract_meta(out_path)
                stat = out_path.stat()
                _meta_db.put(out_path.name, stat.st_mtime, stat.st_size, meta)
            except Exception:
                # The sloppak is already built; a cache-write miss is non-fatal
                # (the scan repopulates it later), but log so it's visible.
                _log.debug("tab_import: metadata cache write failed", exc_info=True)

            _emit({
                "done": True, "progress": 100, "stage": "Complete!",
                "filename": Path(output).name,
                "tracks": ", ".join(arr_names),
                "audio_mode": effective_mode,
                "lyrics_count": len(lyrics_data) if lyrics_data else 0,
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
