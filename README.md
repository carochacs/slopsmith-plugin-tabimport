# Slopsmith Plugin: Import Tab

A plugin for [Slopsmith](https://github.com/byrongamatos/slopsmith) that lets you drag and drop Guitar Pro files directly into the browser to create playable CDLC.

## Features

- **Drag and drop** — drop a .gp3, .gp4, or .gp5 file onto the page
- **Track selection** — auto-detects guitar/bass tracks, lets you choose which to include and assign arrangements (Lead/Rhythm/Bass)
- **Edit metadata** — change title, artist, album before building
- **MIDI audio** — generates audio from the tab using FluidSynth
- **Real-time progress** — shows build progress with stage descriptions

## Installation

```bash
cd /path/to/slopsmith/plugins
git clone https://github.com/byrongamatos/slopsmith-plugin-tabimport.git tab_import
docker compose restart
```

The "Import Tab" link will appear in the navigation bar.

## Requirements

MIDI audio generation requires FluidSynth and a soundfont file. On Windows, Slopsmith Desktop looks for a `.sf2` file in:

```
%APPDATA%\Slopsmith\soundfonts\
```

If no soundfont is found, the build will fail at the audio generation step. A free soundfont such as [GeneralUser GS](https://schristiancollins.com/generaluser.php) placed in that folder is sufficient.

This can be bypassed by providing an audio file to sync.

## How It Works

1. Drag a Guitar Pro file onto the drop zone (or click to browse)
2. The file is parsed — title, artist, album, and tracks are shown
3. Select which tracks to include, choose arrangements
4. Click "Build CDLC"
5. The plugin generates MIDI audio, converts to Rocksmith XML, and packs the output into your library as a `.sloppak` file

## Supported Formats

- Guitar Pro 3 (.gp3)
- Guitar Pro 4 (.gp4)
- Guitar Pro 5 (.gp5)

GP6 and GP7 are not supported (they use a different binary format).

## License

MIT

