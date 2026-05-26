# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Quick start

**Linux / macOS**
```bash
sudo apt install ffmpeg libportaudio2 rubberband-cli   # Debian/Ubuntu; use dnf/pacman equivalents elsewhere
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py /path/to/music/folder
```

**Windows** (run in Windows Terminal, not cmd.exe)
```powershell
winget install Gyan.FFmpeg              # sounddevice bundles PortAudio — no separate install needed
choco install rubberband                # OR download from https://breakfastquay.com/rubberband/ and add to PATH
python -m venv .venv; .venv\Scripts\activate
pip install -r requirements.txt
python main.py C:\path\to\music
```

`main.py` checks for the `rubberband` CLI on PATH at startup and refuses to launch without it — every tempo transition is rendered through it.

On first run the app blocks at the terminal while analyzing every audio file; subsequent runs are instant because results are cached at `~/.automix_cache.json`.

## Keybindings

| Key | Action |
|---|---|
| `Enter` (on a song row) | Load as Now Playing |
| `N` | Queue selected song as Next Track |
| `C` / `F` / `R` | Set Cue / Fade / Restore (opens inline numeric input) |
| `P` | Prepare (renders the transition buffer in the background) |
| `M` | Mix Now (starts the crossfade into the prepared buffer) |
| `Space` | Play / Pause |
| `S` | Stop |
| `Q` | Quit |
| `→` (in Tree) | Load folder's songs into the DataTable and focus it |
| `←` (in DataTable) | Clear songs and return focus to the Tree |

## Architecture

The app is split into three layers that communicate in one direction: `main.py` → `analyzer` → `app` → `engine`.

**`automix/analyzer.py`** — Pure analysis, no audio playback. `analyze_library(root)` scans a folder tree recursively, runs BPM detection (`librosa.beat.beat_track`) and key detection (Krumhansl-Kessler chroma correlation) on each file, and returns `{abs_path: (bpm, key)}`. Decoding is done via pydub/ffmpeg at 22050 Hz mono (analysis only). Cache keys are an MD5 of `path:mtime:size`.

**`automix/audio_engine.py`** — Stateful playback engine built on a single persistent `sd.OutputStream` running at 44100 Hz stereo. The stream callback (`_callback`) runs on a real-time audio thread and must never do I/O — it reads from pre-loaded numpy arrays under a `threading.Lock`. Three states drive the callback: `IDLE` (silence), `PLAYING` (`_fill_playing`), `MIXING` (`_fill_mixing`). During `MIXING`, `_fill_mixing` applies a per-frame linear crossfade envelope between `_now_audio` and `_next_audio`, then transitions back to `PLAYING` when `_mix_pos >= _fade_samples`, promoting `_next_audio` to `_now_audio`. The engine itself owns no time-stretching code — all stretching is done outside in `stretcher.py` before audio is handed to `play()` or `start_mix()`.

**`automix/stretcher.py`** — Thin wrapper around the `rubberband` CLI. `make_transition_buffer(audio, start_rate, fade_seconds, restore_seconds, sample_rate, progress_callback=None)` produces a single contiguous time-stretched buffer for the entire remainder of the incoming track: the first `fade_seconds` of output is at a constant `start_rate` (matches the outgoing track's BPM), the next `restore_seconds` smoothly returns to rate 1.0 along a **smootherstep** curve (`6x⁵ − 15x⁴ + 10x³`), and the rest is at rate 1.0 to the end of the track. Internally builds a 64-anchor timemap from the analytic integral of smootherstep (`S(x) = x⁶ − 3x⁵ + 2.5x⁴`), writes input/timemap to temp files, and shells out to `rubberband -3 --centre-focus -M <map> -D <duration_s> in.wav out.wav` (R3 fine engine; `-M` is the timemap flag, `-D` is the required overall duration; an explicit `(0,0)` timemap row triggers a rubberband NaN bug so the first anchor is always strictly past origin). Output is read back via `soundfile`. When `progress_callback` is supplied the call switches to `Popen`+a daemon thread that drains stderr character-by-character (rubberband uses CR-overwrite for the percent digits), parses `Pass 1:` / `Pass 2:` markers and `<n>%` lines, and forwards a 0.0–1.0 fraction. `rubberband_available()` is the startup probe used by `main.py`.

**`main.py`** — Entry point. Checks for `rubberband` on PATH (hard-fails if missing), parses the music folder argument, prints an ASCII progress bar while `analyze_library` runs, then launches `AutoMixApp`.

**`automix/app.py`** — Textual UI. `AutoMixApp` owns one `AudioEngine` instance. All file loading and rubberband rendering runs in `threading.Thread(daemon=True)` workers; results are pushed back to the UI via `self.call_from_thread(...)`. A 100 ms `set_interval` timer drives `NowPlayingPanel.refresh_progress`. The folder tree uses lazy expansion: subdirectories get a `"__placeholder__"` child node on first render; `on_tree_node_expanded` replaces it with real children. `_songs_in_view` is a list of absolute paths that stays in sync with the `DataTable` rows — `_selected_song()` uses `table.cursor_row` as an index into it. `FolderTree` subclasses `Tree` to intercept right-arrow in its own `on_key`, posting `FolderTree.GoToSongs`; the App handles it to load songs and focus the DataTable.

Tempo transition pipeline: `action_prepare_mix()` reads `cue`, `fade`, and `restore` from the NextTrackPanel, computes `start_rate = now_bpm / next_bpm`, and in a background thread calls `make_transition_buffer(audio[cue:], start_rate, fade, restore, ..., progress_callback=_on_progress)`. The result — the entire remainder of Track B as a single continuous buffer with the rate-ramp pre-baked in — is stored in `self._next_prepared`. `action_mix_now()` just hands that buffer to `engine.start_mix()` and walks away; the engine crossfades from Track A into the buffer, then plays it linearly. There is no post-crossfade swap, no chunk boundaries, no second worker — by construction, the only seam in the output is the track-to-track crossfade itself. `_tick()` detects `MIXING→PLAYING` only to arm the live BPM-display animation, which uses the same smootherstep to track what's actually playing.

Prepare-time BPM animation: the NextTrackPanel shows the displayed BPM sliding from `next_bpm` toward `now_bpm` while Prepare runs. This is driven by `self._prep_progress` (0.0–1.0), updated by the stretcher's `progress_callback` from rubberband's own stderr — not by an elapsed-time clock. `_tick()` reads `_prep_progress`, applies smootherstep, and updates `NextTrackPanel._display_bpm`. By construction the animation lands at `now_bpm` exactly when prep completes, regardless of track length or CPU speed.

Changing C/F/R after Prepare calls `_mark_stale()` which clears `_next_prepared`, resets `_prep_progress`, and shows `[STALE - press P]` on the panel, forcing a re-Prepare before Mix can succeed. `NextTrackPanel._display_bpm` is a `_tick()`-driven override for the prep-animation BPM display; `_render()` uses it when non-`None`.

## Key invariants

- Navigation contract: right arrow in the folder Tree loads songs for the highlighted folder and moves focus to the DataTable (`FolderTree.GoToSongs` message); left arrow in the DataTable clears the song list and returns focus to the Tree. Enter in the Tree loads songs without changing focus. Do not add app-level left/right bindings — they would shadow widget-level handling.
- All mutations to `AudioEngine` state (`_now_audio`, `_next_audio`, `_position`, `_mix_pos`, `state`) must be done inside `self._lock`. The callback also acquires the lock, so keep critical sections short.
- `_next_audio` passed to `start_mix()` must already be rendered by `make_transition_buffer()` (or be the full unmodified track for a non-tempo-matched cue) — the engine plays it verbatim from index 0.
- After `MIXING → PLAYING` transition, `_position` is set to `_mix_pos` (not 0), so playback of the former next track continues from where the crossfade left off.
- `call_from_thread` is the only safe way to touch Textual widgets from the audio/prep worker threads.
- The inline cue/fade text input (`_input_mode`) intercepts keys in `on_key` and calls `event.prevent_default()` + `event.stop()` to suppress bindings while active.
- `make_transition_buffer()` is the **only** place in the project that invokes rubberband. All time-stretching routes through it. The librosa import in `audio_engine.py` is gone; librosa is now used only by `analyzer.py` for BPM/key detection.
- Any change to Cue / Fade / Restore after Prepare must call `_mark_stale()`. The precomputed buffer's contents depend on all three; silently keeping a stale buffer was a pre-rubberband bug and must not return.
- `action_mix_now()` is blocked while `engine.state == MIXING` or `_t_restore_start > 0.0` (i.e. previous transition's crossfade or restoration ramp still running). The prepared buffer's `start_rate` is rendered against `_now_bpm` (the natural BPM of what's playing), which only equals the *actual* playback BPM once any prior ramp has finished. Preparing during a transition is fine; mixing into one is not.
- `_do_load_now()` (Enter on a song row) must clear `_t_restore_start`, `_restore_from_bpm`, and `_restore_to_bpm` before spawning its load worker. Otherwise a prior transition's BPM-display ramp continues animating against the freshly loaded track, and — if Enter interrupted an active crossfade — the next `_tick()` would re-arm the ramp via the MIXING→PLAYING detector. The engine's `play()` only swaps `_now_audio`; it doesn't know anything about app-level restoration state.

## Gotchas

**Textual version ceiling** — `requirements.txt` pins `textual<1.0.0`. Textual 1.x+ (now at 8.x) rewrote internal rendering APIs (`Visual`, `Content`, `render_strips`) in ways that break `Static` subclasses. Do not bump past `0.x`.

**Rich markup in `Static.update()`** — Textual passes strings through Rich markup parsing. Any literal `[`, `]` in displayed text (e.g. `[none]`, `[C]`, filenames) must be escaped as `\\[`. Use `rich.markup.escape()` for user-supplied strings (filenames). Failure produces `AttributeError: 'NoneType' object has no attribute 'render_strips'` at render time.

**WSL audio** — `sounddevice` has no audio devices under WSL without WSLg (WSL2 + `wsl --update`) or manual PulseAudio-over-TCP setup. Run natively on Windows or a Linux desktop instead.

**Progress bar is ASCII** — `main.py` uses `#` and `-` (not `█░`) because Windows terminals default to cp1252, which cannot encode Unicode block characters and raises `UnicodeEncodeError` before the TUI starts.

**Textual key event order** — Within a widget, `on_key` fires *before* the widget's own BINDINGS dispatch; `event.prevent_default()` + `event.stop()` in a subclass `on_key` successfully blocks built-in key actions. App-level `on_key` fires *after* the focused widget has already acted, so intercepting a key before Tree or DataTable handles it requires subclassing the widget, not handling it in the App. Also: `Tree.NodeSelected` fires only on Enter/click, not on arrow-key navigation; use `Tree.NodeHighlighted` for cursor-movement events.

**Rubberband stderr parsing** — Rubberband uses **carriage-return overwrite** for its `<n>%` progress digits (not newlines), so `readline()` and the default line-buffered iterator both block until the pass ends, defeating real-time progress. `_drain_stderr_with_progress` reads stderr one character at a time and chunks on either `\r` or `\n`. Also: in R3 mode Pass 1 ("Studying") is essentially instant (~1% of wall time) and Pass 2 ("Processing") does ~99% — a naive 50/50 weighting causes the BPM display to snap to halfway in the first frame and crawl the remainder over many seconds. The constants `_PASS1_WEIGHT = 0.05` / `_PASS2_WEIGHT = 0.95` in `stretcher.py` reflect this asymmetry; if you ever switch to the R2 engine these need re-calibrating.

**Rubberband CLI flag trap** — `-T` is `--tempo` (a constant stretch factor), NOT the timemap flag. The timemap flag is `-M` (or `--timemap`), and when used it requires an overall stretch factor alongside it, supplied via `-D <duration_seconds>` (or `-t`/`-T`). Without `-D`, rubberband silently produces input-length output with no stretching applied. Also: an explicit `(0, 0)` first row in the timemap triggers a NaN-time-ratio computation at sample 0 that corrupts output; rubberband anchors `(0, 0)` implicitly, so the first explicit anchor must be strictly past origin (handled by `_build_timemap`'s monotonic filter).

## Audio data format

All audio arrays are `(N, 2) float32` in `[-1.0, 1.0]`, at 44100 Hz. pydub decodes to 16-bit PCM integers (`/ 32768.0` normalises). Analysis uses a separate 22050 Hz mono path inside `analyzer.py` and never shares arrays with the engine.
