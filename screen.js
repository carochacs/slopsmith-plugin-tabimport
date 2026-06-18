// Tab Import plugin — screen.js

let _tiTmpPath = null;
let _tiAudioB64 = null;
let _tiAudioFilename = null;
let _tiAudioMode = 'midi'; // 'embedded' | 'autosync' | 'midi'
let _tiHasEmbedded = false;
let _tiRequiresAudio = false; // true for GP6/GP7/GP8 — MIDI synthesis unsupported
let _tiAudioOffset = 0;      // seconds; from /autosync (autosync mode)
let _tiAudioTmpPath = null;  // server-side path to the synced audio (autosync mode)

// HTML-escape helper — values from filenames / server responses are inserted
// into innerHTML below, so they must be escaped to avoid breaking markup (and
// any XSS risk). Defined locally since this plugin ships no shared util.
function esc(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

// ── Drop zone ────────────────────────────────────────────────────────────────
(function () {
    setTimeout(() => {
        const dropzone = document.getElementById('ti-dropzone');
        const fileInput = document.getElementById('ti-file-input');
        if (!dropzone || !fileInput) return;

        dropzone.addEventListener('click', () => fileInput.click());

        dropzone.addEventListener('dragover', (e) => {
            e.preventDefault();
            dropzone.classList.add('border-accent/60', 'bg-accent/5');
        });
        dropzone.addEventListener('dragleave', () => {
            dropzone.classList.remove('border-accent/60', 'bg-accent/5');
        });
        dropzone.addEventListener('drop', (e) => {
            e.preventDefault();
            dropzone.classList.remove('border-accent/60', 'bg-accent/5');
            const file = e.dataTransfer.files[0];
            if (file) tiHandleFile(file);
        });

        fileInput.addEventListener('change', () => {
            if (fileInput.files[0]) tiHandleFile(fileInput.files[0]);
        });
    }, 100);
})();

async function tiHandleFile(file) {
    const ext = file.name.split('.').pop().toLowerCase();
    const supported = ['gp3', 'gp4', 'gp5', 'gpx', 'gp'];
    if (!supported.includes(ext)) {
        alert('Unsupported format. Supported: GP3, GP4, GP5, GPX, GP (GP7/GP8).');
        return;
    }

    const dropzone = document.getElementById('ti-dropzone');
    dropzone.innerHTML = `<p class="text-gray-400 text-sm">Parsing ${esc(file.name)}...</p>`;

    const reader = new FileReader();
    reader.onload = async (e) => {
        const b64 = e.target.result.split(',')[1];
        try {
            const resp = await fetch('/api/plugins/tab_import/upload', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ filename: file.name, data: b64 }),
            });
            const data = await resp.json();

            if (data.error) {
                dropzone.innerHTML = `<p class="text-red-400 text-sm">${esc(data.error)}</p>
                    <button onclick="tiReset()" class="mt-3 text-xs text-gray-500 hover:text-white">Try another file</button>`;
                return;
            }

            _tiTmpPath = data.tmp_path;
            _tiHasEmbedded = !!data.has_embedded_audio;
            _tiRequiresAudio = !!data.requires_audio;
            _tiAudioMode = _tiHasEmbedded ? 'embedded' : 'midi';
            tiShowParsed(data, file.name);
        } catch (err) {
            dropzone.innerHTML = `<p class="text-red-400 text-sm">Upload failed: ${esc(String(err))}</p>
                <button onclick="tiReset()" class="mt-3 text-xs text-gray-500 hover:text-white">Try again</button>`;
        }
    };
    reader.readAsDataURL(file);
}

function tiShowParsed(data, filename) {
    document.getElementById('ti-dropzone').classList.add('hidden');
    document.getElementById('ti-parsed').classList.remove('hidden');
    document.getElementById('ti-progress').classList.add('hidden');
    document.getElementById('ti-result').classList.add('hidden');

    document.getElementById('ti-title').value = data.title || '';
    document.getElementById('ti-artist').value = data.artist || '';
    document.getElementById('ti-album').value = data.album || '';

    // Track list — add is_vocal / is_drums / is_piano badges
    const container = document.getElementById('ti-tracks');
    container.innerHTML = data.tracks.map(t => {
        const checked = t.is_guitar ? 'checked' : '';
        const badge = t.is_drums ? '🥁' : t.is_vocal ? '🎤' : t.is_piano ? '🎹' : '';
        const arrOptions = ['Lead', 'Rhythm', 'Bass', 'Drums', 'Keys', 'Vocals'].map(a =>
            `<option value="${a}" ${t.arrangement === a ? 'selected' : ''}>${a}</option>`
        ).join('');
        return `<div class="flex items-center gap-3 py-2 px-3 rounded-lg bg-dark-600/50">
            <input type="checkbox" data-track="${t.index}" ${checked}
                class="ti-track-check accent-accent">
            <span class="text-sm text-gray-300 flex-1">
                ${badge ? `<span class="mr-1">${badge}</span>` : ''}${esc(t.name)}
                <span class="text-gray-600">(${Number(t.strings) || 0} strings, ${Number(t.notes) || 0} notes)</span>
            </span>
            <select data-track-arr="${t.index}" class="bg-dark-700 border border-gray-700 rounded-lg px-2 py-1 text-xs text-gray-300 outline-none">
                ${arrOptions}
            </select>
        </div>`;
    }).join('');

    // Show GP8 embedded audio banner if present
    const banner = document.getElementById('ti-audio-banner');
    const audioSection = document.getElementById('ti-audio-section');
    const audioToggle = document.getElementById('ti-audio-toggle');
    // Reset the toggle label — a previous run may have left it expanded
    // ("− Hide audio section") even though the section starts hidden here.
    audioToggle.textContent = '+ Add audio file for auto-sync';

    if (_tiHasEmbedded) {
        document.getElementById('ti-sync-count').textContent = data.sync_point_count || 0;
        banner.classList.remove('hidden');
        audioSection.classList.add('hidden');
        // Keep the toggle available so the user can still supply their own
        // audio (e.g. when the embedded track is low quality) — loading an
        // audio file switches the mode to 'autosync' and overrides embedded.
        audioToggle.classList.remove('hidden');
        tiSetAudioMode('embedded');
    } else if (_tiRequiresAudio) {
        // GP6/GP7/GP8 without embedded audio: MIDI synthesis is unsupported,
        // so audio is required. Open the audio section immediately and disable
        // the Build button until a file is attached.
        banner.classList.add('hidden');
        audioSection.classList.remove('hidden');
        audioToggle.classList.add('hidden');
        tiSetAudioMode('midi');
    } else {
        banner.classList.add('hidden');
        audioSection.classList.add('hidden');
        audioToggle.classList.remove('hidden');
        tiSetAudioMode('midi');
    }
    tiUpdateBuildButton();
}

function tiUpdateBuildButton() {
    const btn = document.querySelector('button[onclick="tiBuild()"]');
    if (!btn) return;
    const needsAudio = _tiRequiresAudio && _tiAudioMode !== 'autosync' && _tiAudioMode !== 'embedded';
    btn.disabled = needsAudio;
    btn.title = needsAudio ? 'Attach an audio file — MIDI synthesis is not supported for this format' : '';
    btn.classList.toggle('opacity-40', needsAudio);
    btn.classList.toggle('cursor-not-allowed', needsAudio);

    // Adjust the audio section copy when audio is required (no "skip" option).
    const hint = document.getElementById('ti-audio-hint');
    const skip = document.getElementById('ti-skip-audio-btn');
    if (hint) hint.textContent = _tiRequiresAudio
        ? 'MIDI synthesis is not supported for GP6/GP7/GP8. Attach a matching audio file to continue.'
        : 'Supply a matching audio file for auto-sync, or skip to generate MIDI audio';
    if (skip) skip.classList.toggle('hidden', !!_tiRequiresAudio);
}

// ── Audio mode ───────────────────────────────────────────────────────────────

const _TI_BTN_ACTIVE = 'px-3 py-1.5 rounded-lg text-xs font-medium transition bg-accent text-white';
const _TI_BTN_INACTIVE = 'px-3 py-1.5 rounded-lg text-xs font-medium transition bg-dark-600 text-gray-300 hover:bg-dark-500';

function tiSetAudioMode(mode) {
    _tiAudioMode = mode;

    const midiBtn = document.getElementById('ti-btn-midi');
    const embBtn = document.getElementById('ti-btn-embedded');
    // Highlight whichever of embedded/MIDI is active; for 'autosync' neither is
    // (the build uses the user-supplied audio), so both show inactive.
    if (embBtn) embBtn.className = mode === 'embedded' ? _TI_BTN_ACTIVE : _TI_BTN_INACTIVE;
    if (midiBtn) midiBtn.className = mode === 'midi' ? _TI_BTN_ACTIVE : _TI_BTN_INACTIVE;

    // If user has an audio file loaded, clear it when switching to plain MIDI.
    if (mode === 'midi') tiClearAudio();
    tiUpdateBuildButton();
}

function tiToggleAudioSection() {
    const sec = document.getElementById('ti-audio-section');
    const toggle = document.getElementById('ti-audio-toggle');
    if (sec.classList.contains('hidden')) {
        sec.classList.remove('hidden');
        toggle.textContent = '− Hide audio section';
    } else {
        sec.classList.add('hidden');
        toggle.textContent = '+ Add audio file for auto-sync';
        // Only revert the mode if closing actually discards a loaded autosync
        // file — don't override an explicit MIDI/embedded choice the user made
        // without ever attaching audio.
        const hadAudio = _tiAudioMode === 'autosync';
        tiClearAudio();
        if (hadAudio) tiSetAudioMode(_tiHasEmbedded ? 'embedded' : 'midi');
    }
}

function tiSkipAudio() {
    document.getElementById('ti-audio-section').classList.add('hidden');
    document.getElementById('ti-audio-toggle').textContent = '+ Add audio file for auto-sync';
    tiClearAudio();
    // This control is labelled "Skip — use MIDI", so honour that and select
    // MIDI rather than silently switching to embedded audio.
    tiSetAudioMode('midi');
}

// ── Audio file handling ──────────────────────────────────────────────────────

function tiHandleAudioDrop(e) {
    e.preventDefault();
    document.getElementById('ti-audio-drop').classList.remove('border-accent/60', 'bg-accent/5');
    const file = e.dataTransfer.files[0];
    if (file) tiHandleAudioFile(file);
}

const _TI_AUDIO_EXTS = ['mp3', 'ogg', 'wav', 'flac', 'm4a', 'aac'];

function tiResetAudioDropUI() {
    const drop = document.getElementById('ti-audio-drop');
    if (drop) drop.innerHTML = `<p class="text-gray-400 text-xs">Drop MP3 / OGG / WAV / FLAC here, or click to browse</p>
            <input type="file" id="ti-audio-input" accept=".mp3,.ogg,.wav,.flac,.m4a,.aac" class="hidden"
                   onchange="tiHandleAudioFile(this.files[0])">`;
}

async function tiHandleAudioFile(file) {
    if (!file) return;
    const drop = document.getElementById('ti-audio-drop');

    // Drag-and-drop bypasses the <input accept> filter, so validate the
    // extension here too — otherwise a dropped non-audio file produces a
    // confusing autosync failure (or a large pointless upload).
    const ext = (file.name.split('.').pop() || '').toLowerCase();
    if (!_TI_AUDIO_EXTS.includes(ext)) {
        alert(`Unsupported audio format (.${esc(ext)}). Use: ${_TI_AUDIO_EXTS.join(', ').toUpperCase()}.`);
        tiResetAudioDropUI();
        return;
    }

    drop.innerHTML = `<p class="text-gray-400 text-xs">Loading ${esc(file.name)}...</p>`;

    const reader = new FileReader();
    reader.onload = (e) => {
        _tiAudioB64 = e.target.result.split(',')[1];
        _tiAudioFilename = file.name;
        // Switch to autosync via the setter so the embedded/MIDI buttons stop
        // showing as active (the build will use this audio, not those).
        tiSetAudioMode('autosync');

        tiResetAudioDropUI();
        document.getElementById('ti-audio-name').textContent = file.name;
        document.getElementById('ti-audio-loaded').classList.remove('hidden');
    };
    reader.onerror = () => {
        alert('Could not read the audio file. Please try again.');
        tiClearAudio();
        tiResetAudioDropUI();
    };
    reader.readAsDataURL(file);
}

function tiClearAudio(revertMode = false) {
    // Clear the user-supplied audio state. By default the mode is left to the
    // caller (tiSetAudioMode for explicit MIDI). Pass revertMode=true (e.g. the
    // ✕ clear button) to also drop out of 'autosync' back to the no-user-audio
    // default — embedded if the file has it, else MIDI — so a later build
    // doesn't send audio_mode=autosync with no audio attached.
    _tiAudioB64 = null;
    _tiAudioFilename = null;
    _tiAudioOffset = 0;
    _tiAudioTmpPath = null;
    const loaded = document.getElementById('ti-audio-loaded');
    if (loaded) loaded.classList.add('hidden');
    if (revertMode) {
        tiSetAudioMode(_tiHasEmbedded ? 'embedded' : 'midi');
    }
}

// ── Build ────────────────────────────────────────────────────────────────────

async function tiBuild() {
    if (!_tiTmpPath) return;

    const checks = document.querySelectorAll('.ti-track-check:checked');
    const trackIndices = [...checks].map(c => c.dataset.track);
    if (trackIndices.length === 0) {
        alert('Select at least one track.');
        return;
    }

    // Per-track arrangement selections (idx:Name pairs) so the user's dropdown
    // choices are honoured by the build instead of a name heuristic.
    const arrangements = trackIndices.map(idx => {
        const sel = document.querySelector(`select[data-track-arr="${idx}"]`);
        return sel ? `${idx}:${sel.value}` : null;
    }).filter(Boolean).join(',');

    const title = document.getElementById('ti-title').value.trim();
    const artist = document.getElementById('ti-artist').value.trim();
    const album = document.getElementById('ti-album').value.trim();

    document.getElementById('ti-parsed').classList.add('hidden');
    document.getElementById('ti-progress').classList.remove('hidden');
    document.getElementById('ti-result').classList.add('hidden');

    const setStage = (msg, pct) => {
        document.getElementById('ti-stage').textContent = msg;
        if (pct !== undefined)
            document.getElementById('ti-bar').style.width = pct + '%';
    };

    // Use a local mode for this build attempt so a failed sync falls back to
    // MIDI for *this* run only — the next build can retry autosync.
    let buildMode = _tiAudioMode;

    // If user supplied an audio file, run auto-sync first
    if (buildMode === 'autosync' && _tiAudioB64) {
        setStage('Auto-syncing tab to audio...', 10);
        try {
            const syncResp = await fetch('/api/plugins/tab_import/autosync', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    tmp_path: _tiTmpPath,
                    audio_data: _tiAudioB64,
                    audio_filename: _tiAudioFilename,
                }),
            });
            const syncData = await syncResp.json();
            if (syncData.error) {
                // Server couldn't save the audio at all (disk error, bad
                // payload) — no file to use, fall back to MIDI for this run.
                setStage('Audio upload failed, building with MIDI audio instead...', 20);
                buildMode = 'midi';
            } else {
                // Capture the result so the build can use the real audio.
                // sync_skipped means the file is kept but alignment failed;
                // we still use the audio at offset 0 rather than falling back
                // to MIDI synthesis.
                _tiAudioOffset = syncData.audio_offset ?? 0;
                _tiAudioTmpPath = syncData.audio_tmp_path || null;
                if (syncData.sync_skipped) {
                    setStage(`Using your audio without sync alignment — ${syncData.sync_skipped.split(';')[0]}`, 20);
                } else {
                    setStage(`Synced: ${syncData.sync_point_count ?? 0} points, offset ${(syncData.audio_offset ?? 0).toFixed(3)}s`, 20);
                }
            }
        } catch (err) {
            setStage('Auto-sync failed, continuing with MIDI audio...', 20);
            buildMode = 'midi';
        }
    }

    // Build via WebSocket
    const params = new URLSearchParams({
        tmp_path: _tiTmpPath,
        title, artist, album,
        tracks: trackIndices.join(','),
        arrangements,
        audio_mode: buildMode,
    });
    // Autosync produced a server-side audio file + offset — hand them to the
    // build so it can use the real audio instead of MIDI synthesis.
    if (buildMode === 'autosync' && _tiAudioTmpPath) {
        params.set('audio_offset', String(_tiAudioOffset));
        params.set('audio_tmp_path', _tiAudioTmpPath);
    }

    // Match the backend router prefix (/api/plugins/tab_import) and use wss
    // when the page is served over HTTPS to avoid mixed-content failures.
    const wsProto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${wsProto}//${location.host}/api/plugins/tab_import/ws/build?${params}`);

    ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        if (msg.progress !== undefined)
            document.getElementById('ti-bar').style.width = msg.progress + '%';
        if (msg.stage)
            document.getElementById('ti-stage').textContent = msg.stage;
        if (msg.done) {
            document.getElementById('ti-progress').classList.add('hidden');
            document.getElementById('ti-result').classList.remove('hidden');
            document.getElementById('ti-result').innerHTML = `
                <div class="bg-green-900/20 border border-green-800/30 rounded-xl p-5 text-center">
                    <p class="text-green-400 font-semibold mb-1">CDLC Created!</p>
                    <p class="text-sm text-gray-400">${esc(msg.filename)}</p>
                    <p class="text-xs text-gray-500 mt-1">Tracks: ${esc(msg.tracks)}</p>
                    ${msg.audio_mode === 'embedded' || msg.audio_mode === 'autosync'
                        ? `<p class="text-xs text-accent/70 mt-1">✓ Real audio used — no MIDI synthesis</p>` : ''}
                    <button onclick="tiReset()" class="mt-4 px-4 py-2 bg-dark-600 hover:bg-dark-500 rounded-xl text-sm text-gray-300 transition">Import Another</button>
                </div>`;
        }
        if (msg.error) tiShowError(msg.error);
    };

    ws.onerror = () => tiShowError('Connection lost');
}

function tiShowError(msg) {
    document.getElementById('ti-progress').classList.add('hidden');
    document.getElementById('ti-result').classList.remove('hidden');
    document.getElementById('ti-result').innerHTML = `
        <div class="bg-red-900/20 border border-red-800/30 rounded-xl p-5 text-center">
            <p class="text-red-400 font-semibold mb-1">Build Failed</p>
            <p class="text-sm text-gray-400">${esc(msg)}</p>
            <button onclick="tiReset()" class="mt-4 px-4 py-2 bg-dark-600 hover:bg-dark-500 rounded-xl text-sm text-gray-300 transition">Try Again</button>
        </div>`;
}

// ── Reset ────────────────────────────────────────────────────────────────────

function tiReset() {
    _tiTmpPath = null;
    _tiHasEmbedded = false;
    _tiRequiresAudio = false;
    // Clear audio payload + the loaded-file chip, then reset the mode.
    tiClearAudio();
    _tiAudioMode = 'midi';

    let dropzone = document.getElementById('ti-dropzone');
    dropzone.classList.remove('hidden');
    dropzone.innerHTML = `
        <svg class="w-12 h-12 mx-auto mb-4 text-gray-600" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12"/></svg>
        <p class="text-gray-400 text-sm mb-2">Drag and drop a Guitar Pro file here</p>
        <p class="text-gray-600 text-xs">or click to browse — GP3, GP4, GP5, GPX, GP7/GP8</p>
        <input type="file" id="ti-file-input" accept=".gp3,.gp4,.gp5,.gpx,.gp" class="hidden">`;

    // Clone first to strip any stale listeners accumulated from prior resets,
    // then attach synchronously to the live node. Doing this inline (no
    // setTimeout) avoids a race where rapid double-resets queue several
    // attach callbacks that all bind to the final node (double-upload).
    dropzone.replaceWith(dropzone.cloneNode(true));
    dropzone = document.getElementById('ti-dropzone');
    if (dropzone) {
        dropzone.addEventListener('click', () => document.getElementById('ti-file-input').click());
        dropzone.addEventListener('dragover', e => { e.preventDefault(); dropzone.classList.add('border-accent/60','bg-accent/5'); });
        dropzone.addEventListener('dragleave', () => dropzone.classList.remove('border-accent/60','bg-accent/5'));
        dropzone.addEventListener('drop', e => { e.preventDefault(); dropzone.classList.remove('border-accent/60','bg-accent/5'); const f=e.dataTransfer.files[0]; if(f) tiHandleFile(f); });
        const fi = dropzone.querySelector('#ti-file-input');
        if (fi) fi.addEventListener('change', () => { if (fi.files[0]) tiHandleFile(fi.files[0]); });
    }

    document.getElementById('ti-file-input') && (document.getElementById('ti-file-input').value = '');
    document.getElementById('ti-parsed').classList.add('hidden');
    document.getElementById('ti-progress').classList.add('hidden');
    document.getElementById('ti-result').classList.add('hidden');
    document.getElementById('ti-audio-banner').classList.add('hidden');
    document.getElementById('ti-audio-section').classList.add('hidden');
    // Reset the audio drop zone too, so a stale "Loading…"/file row from a
    // prior autosync attempt doesn't persist into the next import.
    tiResetAudioDropUI();
}
