"""Unit tests for auto_select_tracks heuristics using synthetic track dicts.

auto_select_tracks() calls list_tracks() which calls guitarpro.parse() — both
require a real file on disk.  These tests mock list_tracks at the gp2rs module
level so we can exercise the selection and name-assignment logic against
synthetic inputs without fixture files.
"""

import sys
import unittest
from unittest.mock import MagicMock, patch


def _make_track(name, strings=6, notes=4, is_bass=False, is_drums=False,
                is_piano=False, is_percussion=False, instrument=-1):
    return {
        "index": 0,       # will be overridden per test
        "name": name,
        "strings": strings,
        "notes": notes,
        "is_bass": is_bass,
        "is_drums": is_drums,
        "is_piano": is_piano,
        "is_percussion": is_percussion,
        "instrument": instrument,
    }


def _tracks(*items):
    """Re-index a list of track dicts so index matches position."""
    result = []
    for i, t in enumerate(items):
        result.append({**t, "index": i})
    return result


# Lazily import gp2rs from the core lib path.  If unavailable (CI without the
# core tree) the tests are skipped rather than erroring.
try:
    import importlib, importlib.util
    import pathlib

    # Candidates: sibling slopsmith checkout, or lib/ next to plugin root.
    _here = pathlib.Path(__file__).resolve()
    _candidates = [
        _here.parents[2] / "slopsmith" / "lib" / "gp2rs.py",   # /home/user/slopsmith/lib
        _here.parents[1] / "lib" / "gp2rs.py",                 # plugin_root/lib
        _here.parents[3] / "slopsmith" / "lib" / "gp2rs.py",   # deeper nesting
    ]
    _gp2rs_path = next((p for p in _candidates if p.exists()), None)

    if _gp2rs_path and _gp2rs_path.exists():
        _spec = importlib.util.spec_from_file_location("gp2rs", str(_gp2rs_path))
        gp2rs = importlib.util.module_from_spec(_spec)
        sys.modules.setdefault("gp2rs", gp2rs)
        # guitarpro is only needed for real parse; use MagicMock so that any
        # attribute access (Duration, NoteType, SlideType, …) succeeds without
        # having to enumerate every name referenced in gp2rs's module body.
        if "guitarpro" not in sys.modules:
            _gp_mod = MagicMock(name="guitarpro")
            sys.modules["guitarpro"] = _gp_mod
            sys.modules["guitarpro.models"] = MagicMock(name="guitarpro.models")
        _spec.loader.exec_module(gp2rs)
        _SKIP = False
    else:
        gp2rs = None
        _SKIP = True
except Exception:
    gp2rs = None
    _SKIP = True

_SKIP_REASON = "gp2rs not found (run from slopsmith repo root or with PYTHONPATH set)"


@unittest.skipIf(_SKIP, _SKIP_REASON)
class TestAutoSelectTracks(unittest.TestCase):

    def _run(self, track_list):
        with patch.object(gp2rs, "list_tracks", return_value=track_list):
            return gp2rs.auto_select_tracks("dummy.gp5")

    # ------------------------------------------------------------------
    # Basic role detection
    # ------------------------------------------------------------------

    def test_single_guitar_track(self):
        tracks = _tracks(_make_track("Guitar", strings=6))
        indices, names = self._run(tracks)
        self.assertEqual(indices, [0])
        self.assertEqual(names[0], "Lead")

    def test_single_bass_track_by_flag(self):
        tracks = _tracks(_make_track("Bass Guitar", strings=4, is_bass=True))
        indices, names = self._run(tracks)
        self.assertEqual(indices, [0])
        self.assertEqual(names[0], "Bass")

    def test_single_bass_track_by_name(self):
        tracks = _tracks(_make_track("Bass", strings=4))
        indices, names = self._run(tracks)
        self.assertEqual(indices, [0])
        self.assertEqual(names[0], "Bass")

    def test_drums_track(self):
        tracks = _tracks(_make_track("Drums", is_drums=True, strings=0))
        indices, names = self._run(tracks)
        self.assertEqual(names[0], "Drums")

    def test_piano_track(self):
        tracks = _tracks(_make_track("Piano", is_piano=True, strings=0))
        indices, names = self._run(tracks)
        self.assertEqual(names[0], "Keys")

    # ------------------------------------------------------------------
    # Multi-track naming
    # ------------------------------------------------------------------

    def test_lead_and_rhythm(self):
        tracks = _tracks(
            _make_track("Guitar 1", strings=6),
            _make_track("Guitar 2", strings=6),
        )
        indices, names = self._run(tracks)
        self.assertIn(0, indices)
        self.assertIn(1, indices)
        self.assertEqual(names[0], "Lead")
        self.assertEqual(names[1], "Rhythm")

    def test_three_guitars_gives_combo(self):
        tracks = _tracks(
            _make_track("Guitar 1", strings=6),
            _make_track("Guitar 2", strings=6),
            _make_track("Guitar 3", strings=6),
        )
        indices, names = self._run(tracks)
        self.assertEqual(names[0], "Lead")
        self.assertEqual(names[1], "Rhythm")
        self.assertEqual(names[2], "Combo")

    def test_guitar_and_bass(self):
        tracks = _tracks(
            _make_track("Lead Guitar", strings=6),
            _make_track("Bass Guitar", strings=4, is_bass=True),
        )
        indices, names = self._run(tracks)
        self.assertEqual(names[0], "Lead")
        self.assertEqual(names[1], "Bass")

    def test_two_bass_tracks(self):
        tracks = _tracks(
            _make_track("Bass 1", strings=4, is_bass=True),
            _make_track("Bass 2", strings=5, is_bass=True),
        )
        indices, names = self._run(tracks)
        self.assertEqual(names[0], "Bass")
        self.assertEqual(names[1], "Bass 2")

    def test_two_drum_tracks(self):
        tracks = _tracks(
            _make_track("Drums", is_drums=True, strings=0),
            _make_track("Perc", is_drums=True, strings=0),
        )
        indices, names = self._run(tracks)
        self.assertEqual(names[0], "Drums")
        self.assertEqual(names[1], "Drums 2")

    def test_two_keys_tracks(self):
        tracks = _tracks(
            _make_track("Piano L", is_piano=True),
            _make_track("Piano R", is_piano=True),
        )
        indices, names = self._run(tracks)
        self.assertEqual(names[0], "Keys")
        self.assertEqual(names[1], "Keys 2")

    # ------------------------------------------------------------------
    # Skip logic
    # ------------------------------------------------------------------

    def test_empty_tracks_skipped(self):
        tracks = _tracks(
            _make_track("Empty Track", notes=0),
            _make_track("Guitar", strings=6),
        )
        indices, names = self._run(tracks)
        self.assertNotIn(0, indices)
        self.assertIn(1, indices)

    def test_skip_keywords_filter_tracks(self):
        tracks = _tracks(
            _make_track("String Orchestra", strings=6),
            _make_track("Guitar", strings=6),
        )
        indices, names = self._run(tracks)
        self.assertNotIn(0, indices)
        self.assertIn(1, indices)

    # ------------------------------------------------------------------
    # Fallback when nothing is selected normally
    # ------------------------------------------------------------------

    def test_fallback_selects_non_percussion_tracks(self):
        # A track that matches no keyword and has 4 strings but is not flagged
        # as bass should still be included via the fallback path.
        tracks = _tracks(_make_track("Synth Pad", strings=4))
        indices, names = self._run(tracks)
        self.assertIn(0, indices)

    # ------------------------------------------------------------------
    # Extended-range detection
    # ------------------------------------------------------------------

    def test_7_string_guitar_detected(self):
        tracks = _tracks(_make_track("7 String", strings=7))
        indices, names = self._run(tracks)
        self.assertIn(0, indices)
        self.assertEqual(names[0], "Lead")

    def test_8_string_guitar_detected(self):
        tracks = _tracks(_make_track("8 String", strings=8))
        indices, names = self._run(tracks)
        self.assertIn(0, indices)

    # ------------------------------------------------------------------
    # Full band scenario
    # ------------------------------------------------------------------

    def test_full_band(self):
        tracks = _tracks(
            _make_track("Lead Guitar", strings=6),
            _make_track("Rhythm Guitar", strings=6),
            _make_track("Bass Guitar", strings=4, is_bass=True),
            _make_track("Drums", is_drums=True, strings=0),
            _make_track("Keys", is_piano=True),
        )
        indices, names = self._run(tracks)
        self.assertEqual(len(indices), 5)
        roles = set(names.values())
        self.assertIn("Lead", roles)
        self.assertIn("Rhythm", roles)
        self.assertIn("Bass", roles)
        self.assertIn("Drums", roles)
        self.assertIn("Keys", roles)


if __name__ == "__main__":
    unittest.main()
