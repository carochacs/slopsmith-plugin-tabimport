# Analyze — Tab Import

## Coverage

| Area               | Spec | Plan | Code              | Notes                                |
|--------------------|------|------|-------------------|--------------------------------------|
| Drop UI            | ✅   | ✅   | `screen.html` + JS | Drop + file-pick                    |
| Upload + parse     | ✅   | ✅   | `routes.py`       | base64 → temp file                  |
| Auto-select        | ✅   | ✅   | `routes.py`       | Heuristic name map                  |
| WS build pipeline  | ✅   | ✅   | `routes.py`       | Executor + progress queue           |
| FluidSynth render  | ✅   | ✅   | `gp2midi.py`      | Wrapper over core helpers           |
| RS XML conversion  | ✅   | ✅   | (core `gp2rs`)    | Imported from Slopsmith core        |
| PSARC packaging    | ✅   | ✅   | (core `cdlc_builder`) | Imported from Slopsmith core    |
| Metadata cache     | ✅   | ✅   | `routes.py`       | Best-effort                         |
| Tests              | ❌   | ❌   | —                 | None automated                      |

## Drift

- README "Supported Formats" matches `routes.py` extension whitelist.
- README "How It Works" steps match the build pipeline.
- README "MIDI audio" / FluidSynth claim matches `gp2midi.gp_to_audio`.
- The README does not mention the WebSocket explicitly — users only see the
  progress bar. Internal contract; safe to omit.

## Gaps

1. **Temp dir leak on abnormal disconnect** (Q6). The upload `tmp_path`
   survives if the WS is dropped mid-build. Container restart clears, but
   native / `slopsmith-desktop` installs accumulate.
2. **No collision strategy** (Q5). Re-importing clobbers the prior PSARC.
3. **No FluidSynth presence check** (Q7). Failure is opaque.
4. **No multipart upload path** (Q2). Large GP files balloon during base64
   transit and could OOM.
5. **No unit tests**, particularly for `auto_select_tracks` heuristics.
6. **No GP6/GP7 plan**. Format is rejected with no guidance.

## Recommendations

- **Add a finally-clause cleanup** of the upload `tmp_path` keyed off the WS
  closure (success or error). Track open temp dirs in a per-request set.
- **Configurable collision policy** behind a checkbox: clobber (default) /
  versioned (`_v2`, `_v3`) / cancel.
- **Startup probe** for FluidSynth and the soundfont. If missing, the plugin
  surfaces a banner explaining the missing dependency rather than failing
  silently mid-build.
- **Multipart upload** for files > 10 MB. Keeps base64 path for small files.
- **Unit-test `auto_select_tracks`** with synthetic GP files covering common
  layouts (lead+rhythm+bass, drums-only, vocal track, multiple basses).
- **GP6/GP7** — investigate `pyguitarpro` upstream; stubbed support might
  be cheaper than the user expects.
