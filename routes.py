"""Tab Import plugin — drag and drop Guitar Pro files to create CDLC."""

import asyncio
import base64
import os
import re
import tempfile
from pathlib import Path

from fastapi import WebSocket, WebSocketDisconnect

_get_dlc_dir = None
_extract_meta = None
_meta_db = None


def setup(app, context):
    global _get_dlc_dir, _extract_meta, _meta_db
    _get_dlc_dir = context["get_dlc_dir"]
    _extract_meta = context["extract_meta"]
    _meta_db = context["meta_db"]

    @app.post("/api/plugins/tab_import/upload")
    async def upload_tab(data: dict):
        """Receive a GP file as base64, return parsed track info."""
        filename = data.get("filename", "")
        b64 = data.get("data", "")
        if not filename or not b64:
            return {"error": "No file data"}

        try:
            gp_data = base64.b64decode(b64)
        except Exception:
            return {"error": "Invalid file data"}

        ext = Path(filename).suffix.lower()
        if ext not in ('.gp3', '.gp4', '.gp5'):
            return {"error": f"Unsupported format ({ext}). Only GP3, GP4, GP5 are supported."}

        # Save to temp and parse
        tmp = Path(tempfile.mkdtemp()) / filename
        tmp.write_bytes(gp_data)

        try:
            import guitarpro
            song = guitarpro.parse(str(tmp))

            from gp2rs import auto_select_tracks
            track_indices, name_map = auto_select_tracks(str(tmp))

            tracks = []
            for i, track in enumerate(song.tracks):
                is_selected = i in track_indices
                arr_name = name_map.get(i, "")
                tracks.append({
                    "index": i,
                    "name": track.name,
                    "strings": track.strings and len(track.strings) or 0,
                    "is_guitar": is_selected,
                    "arrangement": arr_name,
                })

            return {
                "title": song.title or Path(filename).stem,
                "artist": song.artist or "Unknown",
                "album": song.album or "",
                "tracks": tracks,
                "tmp_path": str(tmp),
            }
        except Exception as e:
            return {"error": f"Failed to parse: {e}"}

    @app.websocket("/ws/plugins/tab_import/build")
    async def ws_build_tab(websocket: WebSocket, tmp_path: str, title: str = "",
                           artist: str = "", album: str = "", tracks: str = ""):
        """Build CDLC from an uploaded GP file with progress."""
        await websocket.accept()

        dlc = _get_dlc_dir()
        if not dlc:
            await websocket.send_json({"error": "DLC folder not configured"})
            await websocket.close()
            return

        if not Path(tmp_path).exists():
            await websocket.send_json({"error": "File expired — please upload again"})
            await websocket.close()
            return

        # Parse track indices
        try:
            track_indices = [int(x) for x in tracks.split(",") if x.strip()]
        except Exception:
            track_indices = []

        progress_queue = asyncio.Queue()

        def _do_build():
            def report(stage, pct):
                progress_queue.put_nowait({"stage": stage, "progress": pct})

            try:
                gp_path = tmp_path

                report("Parsing Guitar Pro file...", 10)
                from gp2rs import convert_file, auto_select_tracks
                from gp2midi import gp_to_audio
                from cdlc_builder import build_cdlc
                import guitarpro

                song = guitarpro.parse(gp_path)

                if not track_indices:
                    auto_indices, name_map = auto_select_tracks(gp_path)
                else:
                    auto_indices = track_indices
                    # Build name map from track properties
                    name_map = {}
                    for i in auto_indices:
                        t = song.tracks[i]
                        name = t.name.lower()
                        if 'bass' in name:
                            name_map[i] = 'Bass'
                        elif 'rhythm' in name:
                            name_map[i] = 'Rhythm'
                        else:
                            name_map[i] = 'Lead'

                if not auto_indices:
                    progress_queue.put_nowait({"error": "No guitar/bass tracks found"})
                    return

                arr_names = [name_map.get(i, "Lead") for i in auto_indices]
                report(f"Selected {len(auto_indices)} tracks: {', '.join(arr_names)}", 20)

                report("Generating MIDI audio...", 30)
                midi_out = os.path.join(tempfile.mkdtemp(), "midi")
                midi_path = gp_to_audio(gp_path, midi_out)

                report("Converting to Rocksmith XML...", 50)
                xml_dir = tempfile.mkdtemp()
                xml_files = convert_file(gp_path, xml_dir,
                                         track_indices=auto_indices,
                                         audio_offset=0.0,
                                         arrangement_names=name_map)

                t = title or song.title or Path(gp_path).stem
                a = artist or song.artist or "Unknown"
                al = album or song.album or ""
                safe_t = re.sub(r'[<>:"/\\|?*]', '_', t)
                safe_a = re.sub(r'[<>:"/\\|?*]', '_', a)
                output = str(dlc / f"{safe_t}_{safe_a}_midi_p.psarc")

                def on_progress(msg, pct):
                    report(msg, 60 + pct * 0.35)

                report("Compiling SNG and packing PSARC...", 60)
                build_cdlc(
                    xml_paths=xml_files,
                    arrangement_names=arr_names,
                    audio_path=midi_path,
                    title=f"{t} (MIDI)",
                    artist=a,
                    album=al,
                    output_path=output,
                    on_progress=on_progress,
                )

                # Cache metadata
                try:
                    out_path = Path(output)
                    meta = _extract_meta(out_path)
                    stat = out_path.stat()
                    _meta_db.put(out_path.name, stat.st_mtime, stat.st_size, meta)
                except Exception:
                    pass

                progress_queue.put_nowait({
                    "done": True, "progress": 100, "stage": "Complete!",
                    "filename": Path(output).name,
                    "tracks": ", ".join(arr_names),
                })

            except Exception as e:
                import traceback
                traceback.print_exc()
                progress_queue.put_nowait({"error": str(e)})

        loop = asyncio.get_event_loop()
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
                        break
        except WebSocketDisconnect:
            pass

        await websocket.close()
