# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Quick start

**Linux / macOS**
```bash
sudo apt install ffmpeg libportaudio2 rubberband-cli   # or dnf/pacman equivalents
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py /path/to/music/folder
```

**Windows** (Windows Terminal, not cmd.exe)
```powershell
# Python 3.9+ (3.12 is the dev target; the madmom gotcha below is 3.12-specific)
winget install Gyan.FFmpeg              # sounddevice bundles PortAudio
choco install rubberband                # OR breakfastquay.com/rubberband + add to PATH
python -m venv .venv; .venv\Scripts\activate
pip install -r requirements.txt
python main.py C:\path\to\music
```

- **rubberband CLI is mandatory** — `main.py` probes PATH at startup and refuses to launch without it (every tempo transition renders through it).
- First run blocks while analyzing every file; results cache at `~/.automix_cache.json`, so later runs are instant.

### CLI flags
- `--backspin <path>` — SFX one-shot for the `B` transition (default `samples/top_DJ_Rewind_SFX_10.mp3`). Bare name resolved inside `samples/`; relative/absolute used as-is. Must exist or startup hard-fails.
- `--main-device "<name>"` — pins the **master** mix to an output (speakers, or a mixer/PA via the built-in codec's 3.5 mm AUX jack — point it at the Realtek/built-in codec, which auto-routes to the jack). Omit → OS default.
- `--headphones-device "<name>"` — sends the PFL cue (`L`) to headphones. Must be a **separate** device (USB-C/Bluetooth), NOT the 3.5 mm jack (it shares the master codec). Omit → cueing disabled.
- `--list-devices` — print output-capable devices + indices, then exit. Run it **with the AUX cable plugged in** (the jack changes codec naming).

Device names substring-match (case-insensitive) **output-capable** devices only; first match wins; an unknown name hard-fails. Master and headphones must resolve to **different** outputs or the app hard-fails — the guard compares headphones against the *effective* master (`--main-device`, or `sd.default.device[1]` when omitted). Classic setup: `--main-device "Realtek" --headphones-device "B01"`.

## Keybindings

| Key | Action |
|---|---|
| `Enter` (song row) | Load as Now Playing — **only when engine IDLE**. Rejected otherwise; use `N`+`M` to switch during playback. |
| `N` | Queue selected song as Next Track (replaces any queued, discarding an in-flight Prepare; no-op if same path). |
| `C` / `F` / `R` | Set Cue / Fade / Restore (inline numeric input). |
| `P` | Prepare (renders the transition buffer in the background). |
| `M` | Mix Now (crossfade into the prepared buffer). |
| `B` | Backspin: stop the track, play the SFX, start the next at natural tempo from its **raw** cue. Needs a playing track + a **raw** (un-prepared) next track + no transition in progress. |
| `L` | Cue/PFL: toggle pre-listening the queued NEXT track in headphones (from its **raw** cue). Independent of engine state. No-op without `--headphones-device`/a next track. Auto-stops when the NEXT slot changes (`M`/`B`/`N`). |
| `[` / `]` | Seek the cue ∓5 s (while cueing; clamped to `[cue, end]`). |
| `,` / `.` | Master volume −/+ (5% steps, 0–200%, boost-capable, output hard-limited to ±1.0). |
| `9` / `0` | Cue volume −/+ (capped at 100%, no boost). |
| `G` | Toggle the master FX gate (on/off) — a real `Binding`, so it shows in the Footer. Applies the selected effect to the live master; all effects **default to no effect** until the wheel is turned. |
| `1` / `2` / `3` | Select the FX effect: High-pass / Low-pass / Trans (tempo-synced gate). |
| Mouse wheel | Adjust the selected FX intensity (HPF/LPF cutoff sweep; Trans beat division `off`→`1/4`→…→`1/32`) — **only while the gate is engaged**; otherwise it scrolls the tree/song list normally. |
| `Space` | Play/Pause (master only — not the cue). |
| `S` | Stop (master only — not the cue). |
| `Q` | Quit. |
| Tree/DataTable nav | `→`/`Enter`/click drills root → subfolders → songs; `←` walks back up one level; `↑`/`↓` move within folders. See the Navigation contract under Key invariants. |

## Architecture

One-directional layering: `main.py` → `automix/analyzer` → `automix/app` → `automix/engine`. Source modules live in the **`automix/` package**; `main.py` is the only top-level entry point. All audio is `(N, 2) float32` in `[-1, 1]` at 44100 Hz (pydub decodes 16-bit PCM, `/ 32768` normalises). Analysis uses a separate 22050 Hz mono path that never shares arrays with the engine.

- **`automix/analyzer.py`** — Pure analysis (no playback). `analyze_library(root)` → `{abs_path: {bpm, key, beats, downbeats}}`. librosa `beat_track` for BPM/beats; Krumhansl-Kessler chroma for key. Downbeats are **heuristic**: pick the phase `p ∈ {0,1,2,3}` maximizing summed 40–150 Hz STFT energy at every 4th beat (assumes 4/4). `beats`/`downbeats` are sample indices at 44100 Hz (usable directly against `engine.position`). Cache: `~/.automix_cache.json`, shape `{"version": 2, "entries": {md5(path:mtime:size): record}}`; version mismatch → silent re-analyze.
- **`automix/audio_engine.py`** — Stateful playback on one persistent 44100 Hz stereo `sd.OutputStream` (`device=None` → OS default; an index pins the master). States: `IDLE` / `PLAYING` / `MIXING`. `_callback` runs on the real-time audio thread — no I/O, reads pre-loaded numpy under `_lock`. `MIXING` applies a per-frame linear crossfade `_now_audio` → `_next_audio`, then promotes to PLAYING. `start_mix(buf, fade, scheduled_start_sample=...)`: with a scheduled sample it stays PLAYING and `_fill_playing` splits the chunk at the trigger (no silence seam). Owns no time-stretching. Owns one `MasterFx` (`_fx`): `_callback` calls `_fx.process(outdata)` after the fill and before the `_volume` trim; the `set_fx_*`/`fx_state` wrappers mutate it under `_lock`.
- **`automix/stretcher.py`** — The **only** rubberband caller. `make_transition_buffer(audio, start_rate, fade_s, restore_s, sr, progress_callback=None)` → one contiguous buffer for the whole remainder of Track B: `fade_s` at constant `start_rate`, then `restore_s` ramping to rate 1.0 along smootherstep, then 1.0 to the end. Builds a 64-anchor timemap and shells `rubberband -3 --centre-focus -M <map> -D <dur> in out`. `progress_callback` drives the prep BPM animation. `rubberband_available()` is the startup probe.
- **`automix/transition.py`** — Pure skip-vs-stretch planner. `plan_transition(now_bpm, next_bpm, fade)` → `TransitionPlan(skip, start_rate, matched_bpm, drift_beats, relation)`. Octave-folds `r = now/next` to `r_eff` (closest to 1.0 in log space, ≤ 1 octave) so half/double-time pairs fold to ~1.0. `drift_beats = (next_bpm/60)·|r_eff−1|·fade`; `skip = drift_beats ≤ MAX_DRIFT_BEATS` (0.25). `start_rate = r_eff` feeds the stretcher. `tempo_compatible()` (shares `fold_ratio`) drives the song-list `:)` gutter.
- **`automix/fx.py`** — `MasterFx`, the master performance FX applied in the engine callback. Three selectable effects (one active at a time), all defaulting to **no effect**: `hpf`/`lpf` are **4th-order** (24 dB/oct) scipy Butterworth biquads whose cutoff sweeps on a log scale with `intensity ∈ [0,1]` (`amt=0` bypasses; default 0). Ranges are kept inside the audible band (HPF 300→4000 Hz, LPF 10000→250 Hz) so the whole knob is perceptible — a sub-bass-anchored or gentle 2nd-order sweep felt dead until ~90%. The HPF floor is 300 Hz (not lower) so the cut lands in the low-mids within the first fifth of the wheel; below ~250 Hz a high-pass is inaudible on most speakers. `trans` is a hard 50%-duty gate synced to `bpm` (pushed by `_tick` only while the gate is engaged), the wheel stepping `TRANS_DIVISIONS = [0,4,8,16,32]` where **index 0 (division 0) = OFF** (default), with ~2 ms declick ramps at the edges. **Biquad coefficients (`_b`/`_a`) are computed in `_recompute_filter()` from the param setters (UI thread, under the engine lock) — NEVER in `_callback`, which only runs `lfilter`** (`None` coeffs = bypass). Filter `zi` and the gate phase persist across blocks for continuity on small wheel steps, but are reset on **effect-type change** (`set_type`), **gate-enable** (`set_enabled` off→on), and track (re)start (`reset()`, from `play()`/`stop()`) — each is a discontinuity where carried state would click. **`set_type` also zeroes the *outgoing* effect's intensity** (HPF/LPF amount → 0, Trans → off), so only the currently selected effect is ever non-zero and re-selecting any effect starts at no-effect (no silent re-attack). Filter `zi` seeds at **rest (zeros)**, NOT `lfilter_zi` (the unit-step state would inject a spurious transient on audio starting near zero); its shape tracks `_FILTER_ORDER`. Recursion runs in float64 (float32 biquads build an audible noise floor at low cutoffs).
- **`automix/cue_player.py`** — `CuePlayer`, the PFL monitor: an independent second `sd.OutputStream` on a separate device. **Fully decoupled** from `AudioEngine` (own stream/lock/buffer/playhead, no MIXING state). `play((N,2) @ 44100)`, `seek(±s)` clamped, stops silent at end (no loop). On `PortAudioError` falls back to the device samplerate + `np.interp` resample. All PortAudio failures swallowed → `is_dead`.
- **`main.py`** — argparse entry point (flags above). Resolves devices via `_resolve_output_device`; hard-fails on a bad sample, bad device, master==headphones, or missing rubberband; prints an ASCII progress bar while analyzing; launches `AutoMixApp`.
- **`automix/app.py`** — Textual UI; owns one `AudioEngine` + one optional `CuePlayer`. File loads and rubberband renders run in daemon threads → results via `call_from_thread`. A 100 ms `set_interval` drives `_tick()` (progress, phase banner, BPM ramps, deferred swaps). Lazy folder tree; `_songs_in_view` mirrors the DataTable rows; the leading `Mix` gutter (`:)`) is refreshed by `_refresh_match_markers()`. FX controls live in `on_key` (`G` gate / `1`-`3` select) + the mouse-wheel handlers (`_fx_wheel`); `_tick` pushes the live tempo to the engine FX and the NowPlaying panel shows the active effect.
- **`automix/banner.py`** — Top banner widget (`Banner(Static)`); replaces Textual's stock `Header` in `app.compose`. Renders the AUTOMIX cfonts "block" wordmark beside a single static pixel-art portrait using Unicode half-blocks (`▀`/`▄`, 2 px per cell), built by overriding `render()` to assemble a `rich.text.Text` directly — NOT via `Static.update()` markup (sidesteps the `[`/`]` escaping trap). Background cells emit a space so `#banner`'s colour shows; `_crop_grid` trims blank borders; `on_mount` sets height from `banner_height()` (tallest column + 2, both columns vertically centred), so swapping art of any size needs no CSS edit. An empty `GRID` (the `--no-image` mode) renders the wordmark alone. A small `HH:MM:SS` clock (`_add_clock`) ticks top-right via a 1 s `set_interval`.
- **`automix/banner_art.py`** — GENERATED pure-data module (`WORDMARK`/`PALETTE`/`GRID`/`BACKGROUND`) — do not hand-edit. Rebuilt by `scripts/pixart_image_integrator.py <image> [--recolor] [--rows N] [--no-image] [--bg auto|none|#hex] [--grid N|WxH]`: center-samples the PNG to a small grid (cropped to the content, sized to `--rows` ≈ wordmark height), keys out the background to transparent, and (with `--recolor`) snaps colours to the neon UI palette. **Transparency: a PNG with an alpha channel is keyed by its alpha** (`--bg auto`); otherwise the border background is detected (median + cluster test, tolerant of noisy/gradient backgrounds). **`--no-image` bakes empty art (`GRID=[]`, empty `PALETTE`); `automix/banner.py` then renders the wordmark alone.** Static images only (no animation). Works on any low-res flat-colour pixel-art PNG. **Pillow is build-time only (NOT a runtime dep); numpy is reused.** Bundled sample `assets/sample.png`.

### Transition pipeline (automix/app.py)
- **Prepare** (`action_prepare_mix`): reads C/F/R, calls `plan_transition`, stashes the plan in `_next_plan` (lockstep with `_next_prepared`). **STRETCH** (`not skip`) renders `make_transition_buffer(audio[cue:], start_rate, …)` in a thread; **SKIP** stashes raw `audio[cue:]`, no rubberband.
- **Mix** (`action_mix_now`): bar-aligns by firing on Track A's next downbeat after `position + 100 ms` (sample 0 of the buffer = Track B's snapped downbeat). Falls back to immediate start when either side lacks downbeats or the cue wasn't snapped. Deferred mixes swap now-playing metadata via `_pending_now_swap` only when `_tick` sees PLAYING→MIXING; immediate mixes swap synchronously. STRETCH arms the restore ramp (`_restore_from_bpm`/`_to_bpm`); SKIP leaves them at 0.0.
- **Backspin** (`action_backspin`, `B`): bypasses `plan_transition` — builds `concat([_backspin_audio, next_audio[raw_cue:]])` and `engine.play()`s it. SFX preloaded at mount. Stays PLAYING (no ramp, no transition detectors).
- **Cue/PFL** (`action_cue_toggle`, `L`): a worker decodes `next[raw_cue:]` → `cue.play()`. Independent of engine state.
- Prep BPM animation (STRETCH only) is driven by rubberband's `progress_callback` via `_prep_progress`, not a clock — it lands on `matched_bpm` exactly when prep finishes. The restore BPM ramp is wall-clock (`_t_restore_start`) but frozen while `engine.paused`. `_mark_stale()` fires on any C/F/R change after Prepare.

## Key invariants

- **Engine state under lock.** All mutations to `_now_audio`/`_next_audio`/`_position`/`_mix_pos`/`state` go inside `_lock` (the callback takes it too — keep sections short). `call_from_thread` is the only safe way to touch Textual widgets from worker threads.
- **Buffers play verbatim from index 0.** `_next_audio` for `start_mix()` must already be rendered by `make_transition_buffer()` (or be a full unmodified track). After MIXING→PLAYING, `_position = _mix_pos` (not 0), so the incoming track continues where the crossfade left off.
- **rubberband is invoked only in `make_transition_buffer()`.** All time-stretching routes through it (librosa now lives only in `automix/analyzer.py`).
- **Stretch rate is always `plan.start_rate` (`r_eff`)**, never raw `now_bpm/next_bpm` — recomputing it raw reintroduces the half/double-time bug (a 64 BPM track stretched to 128). Always go through `plan_transition()`. `MAX_DRIFT_BEATS` (0.25, `automix/transition.py`) is the single skip/stretch tunable (window scales as `|Δbpm| ≤ 60·0.25/fade`).
- **`_next_plan` and `_next_prepared` move in lockstep** — reset BOTH to `None` together (`action_stop`, `_handle_track_finished`, `action_load_next`, `_mark_stale`, and after consumption in `action_mix_now`). A `skip == True` buffer is raw cue audio, so `action_mix_now` arms no restore ramp for it (`_restore_from_bpm`/`_to_bpm` stay 0.0).
- **Any C/F/R change after Prepare must call `_mark_stale()`** (the buffer depends on all three; keeping a stale buffer was a real pre-rubberband bug).
- **`_prep_epoch` is the single source of truth for "is this prep worker still relevant?"** Bump it in every prep-invalidating action: `action_load_next`, `action_prepare_mix`, `_mark_stale`, `action_stop`, `_do_load_now`, `_handle_track_finished` (each worker captures it and aborts on mismatch). Actions that can leave a **completed** buffer dangling (`action_stop`, `_handle_track_finished`) must ALSO null `_next_prepared`/`_next_plan` and reset the panel to `[not prepared]` — the epoch only orphans in-flight workers.
- **`_do_load_now()` must clear `_t_restore_start`/`_restore_from_bpm`/`_restore_to_bpm`/`_mix_scheduled` and bump `_prep_epoch`** (clearing `_preparing`/`_prep_animating`/`_prep_progress`) before its load worker — else a prior ramp animates against the new track, or an in-flight Prepare (rendered against the old `_now_bpm`) installs a wrong-tempo buffer.
- **Now-playing changes ONLY via `Enter` (engine IDLE) or a completed Mix.** `Enter` while playing is rejected by `on_data_table_row_selected`, so `_do_load_now` is never called against a non-IDLE engine. `action_prepare_mix`/`action_mix_now` both require `state != IDLE` (Prepare also `_now_bpm > 0`) — keep these guards on any new entry points.
- **Natural track-end:** when the engine reaches IDLE on its own, `_tick()` calls `_handle_track_finished()` exactly once (guarded by `_now_path is not None`, so it doesn't re-fire after `action_stop`, which already nulls it).
- **`action_mix_now` is blocked while `state == MIXING` or `_t_restore_start > 0.0`** (a buffer's `start_rate` only equals actual playback BPM once prior ramps finish — preparing during a transition is fine, mixing into one is not). The phase-specific block messages mirror the `NowPlayingPanel._phase` banner `_tick` keeps in sync (MIXING / WAITING-for-downbeat / RESTORING / Ready).
- **Bar alignment requires BOTH tracks' downbeats AND a snapped cue.** Otherwise `action_mix_now` falls back to immediate start (the status line reports which path was taken).
- **`NextTrackPanel` keeps two cue values, written only by `set_cue()`:** `raw_cue` (unsnapped — `0.0` or exactly what was typed) and `cue` (bar-snapped to the nearest downbeat, flag `cue_snapped`). **Backspin reads `raw_cue`; Prepare/Mix read the snapped `cue`** — so a `0:00` cue backspins from the start while the crossfade still bar-aligns. Never write `_cue`/`_cue_raw` directly.
- **Backspin (`B`) requires a raw next track and bypasses the prepare pipeline.** Guards: track playing, `_next_path` set, `not _preparing`, `_next_prepared is None`, `state != MIXING`, `_t_restore_start == 0.0`, `_backspin_audio` loaded. Arms no restore ramp; MUST offset `_now_downbeats` by `len(_backspin_audio) - cue_sample` (else a later bar-aligned mix lands off the bar). Calls `engine.play()` (stays PLAYING — do not route through the MIXING transition detectors).
- **The cue (`CuePlayer`, `L`) is a second, independent output stream** — never couple it to `AudioEngine` (separate stream/lock/buffer, no `engine.state` guard; cueing in any state is the point). It decodes the **raw** cue, never `_next_prepared` (a STRETCH buffer is the wrong tempo). `_cue_epoch`/`_cue_loading` mirror `_prep_epoch`; `_stop_cue()` is the single auto-stop path (called from `action_mix_now`, `_apply_backspin`, `action_load_next` on a real change) — transport (`S`/`Space`) and C/F/R never touch it. Device loss is swallowed → `is_dead` → a one-shot status line via `_cue_dead_reported`.
- **Key matching uses `event.character`, not `event.key`,** for `[`/`]` seek and the `,`/`.`/`9`/`0` volume keys — so AltGr-composed characters work on non-US layouts (e.g. Italian).
- **Software volume lives in the audio layer:** `AudioEngine._volume` (`[0, MAX_GAIN=2.0]`, boost-capable, output hard-limited to ±1.0 when > 1.0) and `CuePlayer._volume` (`[0, 1]`, no boost — ear safety) are applied inside their callbacks. Independent of the OS default-device volume keys — the whole point of pinning a non-default master.
- **Master FX DSP lives in the engine callback under `_lock`** (`MasterFx.process` — `lfilter`/gate only, no coefficient computation), never in the app. The app only mutates params via the engine's `set_fx_*` wrappers (also under `_lock`); those wrappers run `butter()` so it stays off the real-time thread. `action_stop` disengages the gate (so the next track never starts mid-effect). Selecting a different effect (`set_type`) zeroes the previous one, so **at most one effect is ever non-zero** (the selected one). The app keeps an `_fx_enabled` mirror of the gate so `_tick` only pushes tempo while engaged. The mouse wheel diverts to FX intensity **only while the gate is engaged** (and never during inline C/F/R input) — `FolderTree`/`SongTable` (a `DataTable` subclass) intercept the scroll in their own `on_mouse_scroll_*` (a scrollable widget consumes the event before it bubbles to the App), each calling `_fx_wheel`, which returns False when the gate is off so normal list scrolling still works. `G` is a real `Binding` (`action_fx_gate`) so it shows in the Footer (`g` is a plain letter → layout-safe); `1`/`2`/`3` stay in `on_key` matched on `event.character`, NOT as `BINDINGS`, because declarative bindings match `event.key`, which is not layout-safe for digits on the Italian keyboard (same reason the `,`/`.`/`9`/`0` volume keys live in `on_key`).
- **Navigation contract** (folder browser, three-level drill-down): **root display** → **subfolder display** → **song browsing**. `→` (right-arrow, `on_key`/`GoToSongs`) only ever drills in: on the **root** it expands (highlight the first subfolder, focus stays on the Tree); on a **subfolder** it collapses any open sibling (accordion), expands + loads songs, and focuses the DataTable **only if the folder has songs** (`on_folder_tree_go_to_songs` guards on `_songs_in_view` — a song-less folder keeps focus on the Tree, not an empty table). `←` walks up one level (DataTable → Tree via `_exit_song_browsing()`, or the Tree collapses the cursor node's parent). Up-arrow never re-lands on the root. **`Enter`/click are a toggle** (`on_tree_node_selected`): a **second** activation on an already-open folder *exits* it (root → collapse to root display + clear songs; subfolder → `_exit_song_browsing()`), and opening a subfolder first **collapses any open sibling** (accordion — at most one subfolder open at a time, so the previously browsed folder's arrow returns to `>`). Because `Enter` moves focus to the next level, its toggle only fires in the edge cases where focus stays put (empty root, or an empty subfolder — which keeps focus on the Tree rather than jumping into an empty table). **Every ancestor-collapse routes through `_collapse_subtree()`** (module-level helper): Textual's `node.collapse()` does NOT cascade, so collapsing an ancestor (left-arrow's `parent`, `_exit_song_browsing`, the sibling accordion in both the `→` and Enter paths, and the root second-activation) while a descendant stays expanded would leave a stale `▼` and spilled grandchildren when the ancestor reopens — `_collapse_subtree` collapses descendants depth-first before the node. `FolderTree` subclasses `Tree`, sets `auto_expand = False`, intercepts right/left/up/space in its own `on_key`, and overrides `_on_click` so a click on the expand **arrow** routes through `select_cursor` (same as clicking the name — no bare toggle). Songs directly in the root folder are not browsable by design. Do NOT add app-level left/right bindings (they'd shadow widget-level handling).
- The inline C/F input (`_input_mode`) intercepts keys in `on_key` with `prevent_default()` + `stop()` to suppress bindings while active.
- **Library dict shape is `{path: {bpm, key, beats, downbeats}}`** (a record dict, NOT a `(bpm, key)` tuple). Use `empty_record()` from `automix/analyzer.py` as the `library.get(path, ...)` default so garbled cache entries degrade to an empty record, not a KeyError.

## Gotchas

**Textual version ceiling** — `requirements.txt` pins `textual<1.0.0`. Textual 1.x+ (now at 8.x) rewrote internal rendering APIs (`Visual`, `Content`, `render_strips`) in ways that break `Static` subclasses. Do not bump past `0.x`.

**Rich markup in `Static.update()`** — Textual passes strings through Rich markup parsing. Any literal `[`, `]` in displayed text (e.g. `[none]`, `[C]`, filenames) must be escaped as `\\[`. Use `rich.markup.escape()` for user-supplied strings (filenames). Failure produces `AttributeError: 'NoneType' object has no attribute 'render_strips'` at render time. (This is why the progress bars built by `_progress_segment` use an escaped leading `\[` — an unescaped `[#...` is parsed as a colour tag and the bar silently vanishes.)

**WSL audio** — `sounddevice` has no audio devices under WSL without WSLg (WSL2 + `wsl --update`) or manual PulseAudio-over-TCP setup. Run natively on Windows or a Linux desktop instead.

**Console output must be ASCII** — Windows terminals default to cp1252. The `main.py` progress bar uses `#`/`-` (not `█░`), and *every* string printed outside the Textual TUI — argparse `help=`, `sys.stderr` errors, status text — must avoid em-dashes/Unicode (`—` → `-`), or you get `�` mojibake or `UnicodeEncodeError` before the TUI starts. (Rich-rendered text inside Textual is fine.)

**MME truncates device names to 31 chars** — `sd.query_devices()` lists each output under several host APIs; the MME copy's name is cut to 31 chars, so a long `--main-device`/`--headphones-device` substring can skip the MME entry and match the DirectSound/WASAPI copy (same physical device, different index). `_resolve_output_device` returns the first output-capable match — prefer a short substring (`Realtek`, `B01`); `--list-devices` shows the indices.

**Textual key event order** — Within a widget, `on_key` fires *before* the widget's own BINDINGS dispatch; `event.prevent_default()` + `event.stop()` in a subclass `on_key` blocks built-in key actions. App-level `on_key` fires *after* the focused widget has acted, so intercepting a key before Tree or DataTable handles it requires subclassing the widget, not handling it in the App. Also: `Tree.NodeSelected` fires only on Enter/click, not on arrow-key navigation; use `Tree.NodeHighlighted` for cursor-movement events.

**Rubberband stderr parsing** — Rubberband uses **carriage-return overwrite** for its `<n>%` progress digits (not newlines), so `readline()` and the default line-buffered iterator both block until the pass ends. `_drain_stderr_with_progress` reads stderr one character at a time and chunks on `\r` or `\n`. Also: in R3 mode Pass 1 ("Studying") is ~1% of wall time and Pass 2 ("Processing") does ~99% — the constants `_PASS1_WEIGHT = 0.05` / `_PASS2_WEIGHT = 0.95` reflect this asymmetry and need re-calibrating if you switch to the R2 engine.

**Rubberband CLI flag trap** — `-T` is `--tempo` (a constant factor), NOT the timemap flag. The timemap flag is `-M` (`--timemap`), and it requires an overall stretch factor alongside it via `-D <duration_seconds>`. Without `-D`, rubberband silently produces input-length output with no stretching. Also: an explicit `(0, 0)` first timemap row triggers a NaN-time-ratio bug at sample 0; rubberband anchors `(0, 0)` implicitly, so the first explicit anchor must be strictly past origin (handled by `_build_timemap`).

**Downbeat detection is heuristic** — `automix/analyzer.py` runs librosa's `beat_track`, then picks the phase (`0,1,2,3`) maximizing summed 40–150 Hz STFT energy at every 4th beat. Good on 4/4 dance/pop; less reliable on jazz, 3/4, or kick-less tracks. A track with no detected beats gets empty `beats`/`downbeats`, and any mix involving it falls back to immediate (non-aligned) start.

**madmom on Python 3.12 is impractical** — madmom (and BeatNet, which needs it) ships a 2018 PyPI release using `np.float` (removed in numpy 1.24) and needs MSVC build tools. That route demands `numpy<2` + Build Tools; the librosa heuristic is the conscious trade-off for simple deps.

**Cache schema versioning** — The cache at `~/.automix_cache.json` is `{"version": 2, "entries": {...}}`. The loader rejects any other shape and re-analyzes from scratch. Bump `CACHE_VERSION` whenever the per-track record's required fields change (the first launch after a bump pays the full re-analysis cost).

## Testing

No pytest/unittest — `tests/` holds standalone probe scripts, each run directly and printing `PASS`/`All ... passed`. Run the suite:

```powershell
Get-ChildItem tests\probe_*.py | ForEach-Object { .\.venv\Scripts\python.exe $_.FullName }
```

| Script | Covers |
|---|---|
| `probe_analyzer.py` | Downbeat detection on real tracks (needs `music/`) |
| `probe_engine_split.py` | `start_mix` scheduled-start chunk-split; master `_volume` gain/clamp/hard-limit |
| `probe_prep_epoch.py` | Prep-supersession races (N / P / stale / Enter during Prepare) |
| `probe_cue_reset.py` | Cue snap + reset-on-N |
| `probe_phase_banner.py` | `NowPlayingPanel` phase line + "outgoing → incoming" dual name |
| `probe_conflicts.py` | Track-end cleanup, P/M guards, deferred swap, pause-during-ramp, Stop, self-mix |
| `probe_transition_plan.py` | `plan_transition` skip-vs-stretch, octave folding, drift formula, half-time guard |
| `probe_skip_mix.py` | Skip stores raw buffer + no restore ramp; stretch arms ramp from `matched_bpm` |
| `probe_backspin.py` | Backspin guards, `[SFX + cue]` buffer, swap, no ramp, downbeat offset |
| `probe_cue.py` | `CuePlayer` position/stop/seek/dead/gain + `_resample`; app toggle, `_cue_epoch`, volume keys. Device I/O is **manual only** (needs a real cue device). |
| `probe_fx.py` | `MasterFx` HPF/LPF attenuation, bypass, tempo-synced Trans gate period/duty/phase-continuity, `adjust`/`describe`; engine callback applies FX + `play()` resets state |
| `probe_banner.py` | Banner art data well-formed, blank-border crop, half-block render height, wordmark gradient, +2/centred composition, clock overlay, no-image (empty GRID) path, alpha keying; `theme_palette`/`nearest_color` recolour helpers (imports `scripts/pixart_image_integrator.py`) |
| `probe_tree_nav.py` | Folder-tree navigation: right/Enter drill-down, accordion (one subfolder open), collapse cascade to hidden grandchildren, song-less folder keeps tree focus, left-arrow exit, up-arrow skips root, second-activation toggle. Mounts the real app via `run_test()` (Pilot) with a stubbed engine. |
| `probe_e2e_alignment.py` | Full bar-alignment against real tracks (needs `music/`) |

**Headless harness pattern** — engine and UI are tested without an audio device or mounted app: build `AudioEngine.__new__(...)` / `AutoMixApp.__new__(...)`, set state fields manually (skips opening the stream), stub `query_one`/`_status`, and drive `_callback`/`_tick`/actions directly with synthetic numpy audio. Keep this pattern — a real stream/mount can't run headless. When you add a field to `AudioEngine`/`CuePlayer.__init__` or new `AutoMixApp` state, update every `__new__` harness (and the `FakeEngine`/`FakeCue` stand-ins) too, or it surfaces as an `AttributeError` from the driven code, not a clean skip. (E.g. `AudioEngine._fx` must be set in every engine harness since `play()`/`stop()`/`_callback` touch it; a `FakeEngine` driven through `_tick`/`action_stop` needs the FX no-ops + `fx_state`, and a `build_app` harness needs the `_fx_enabled` flag `_tick`/`action_stop` read.) **Exception — widget/navigation tests** (`probe_tree_nav.py`) need a real mounted widget tree (focus moves, `Tree.NodeSelected` messages, line layout), which the `__new__` harness can't provide; those mount the app headlessly via Textual's `run_test()` Pilot, patching `app_mod.AudioEngine`/`CuePlayer` to no-op stubs and neutering `_tick` so no audio device is touched.

`tests/diagnose_progress.py` is a diagnostic (not a test): runs `make_transition_buffer` on a real track and logs every rubberband stderr chunk + parsed progress to `tests/progress_log.txt`.
