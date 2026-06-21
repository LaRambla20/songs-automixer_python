<div align="center">
  <img src="assets/sample.png" alt="AutoMix cat" width="160">
  <h3>AutoMix</h3>
</div>

<p align="center"><i>Being a DJ has never been this easy</i></p>

<p align="center">
  <img alt="last commit" src="https://img.shields.io/github/last-commit/LaRambla20/songs-automixer_python?style=flat-square&color=2ecc40">
  <img alt="license" src="https://img.shields.io/badge/license-MIT-2ecc40?style=flat-square">
</p>

<h2></h2>

A terminal-based music auto-mixer for Linux, macOS, and Windows. Load a music folder, browse tracks, and seamlessly crossfade between songs with automatic tempo synchronisation. Includes DJ-style features: live master FX (high-pass / low-pass filter and a tempo-synced gate), a backspin/rewind transition, separate master/headphone output routing with pre-listen cueing (PFL), in-app volume control, and a hands-off **auto-mix** that crossfades into the next track as the current one nears its end.

![AutoMix terminal UI](assets/screenshot0.png)

## Requirements

- Python 3.9+
- **`ffmpeg`** — audio decoding
- **`rubberband`** CLI — tempo stretching for transitions. `main.py` checks for it on your `PATH` at startup and refuses to launch without it.
- On **Linux**, PortAudio is also needed for playback (`sounddevice` bundles it on Windows and macOS).

### Install the system dependencies

**Debian / Ubuntu**
```bash
sudo apt install ffmpeg libportaudio2 rubberband-cli
```

**Fedora**
```bash
sudo dnf install ffmpeg portaudio rubberband
```

**Arch**
```bash
sudo pacman -S ffmpeg portaudio rubberband
```

**macOS** (Homebrew)
```bash
brew install ffmpeg rubberband
```

**Windows** (run in Windows Terminal, not `cmd.exe`)
```powershell
winget install Gyan.FFmpeg              # sounddevice bundles PortAudio — no separate install needed
choco install rubberband                # OR download from https://breakfastquay.com/rubberband/ and add it to PATH
```

## Installation

```bash
# Linux / macOS
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```
```powershell
# Windows
python -m venv .venv; .venv\Scripts\activate
pip install -r requirements.txt
```

> **WSL note:** `sounddevice` has no audio device under WSL without WSLg (WSL2 + `wsl --update`) or a manual PulseAudio-over-TCP setup. Run natively on Windows or a Linux desktop instead.

### Troubleshooting: SSL certificate errors during `pip install`

If `pip install` fails with `CERTIFICATE_VERIFY_FAILED` / `unable to get local issuer certificate`, your network (often a corporate proxy or antivirus) is intercepting TLS with a certificate that Python's bundled store doesn't recognise. Two fixes:

- **Use the OS certificate store** (recommended — pip ≥ 23.2, Python ≥ 3.10):
  ```bash
  pip config set global.use-feature truststore
  ```
  Then re-run the install normally. On Windows this trusts the same certificates as the rest of the system.

- **One-off workaround** — trust PyPI's hosts for a single command:
  ```bash
  pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org -r requirements.txt
  ```

## Usage

```bash
python main.py /path/to/music/folder
```

To use a different backspin SFX for the `B` transition (default `samples/top_DJ_Rewind_SFX_10.mp3`):

```bash
python main.py /path/to/music/folder --backspin backspin_09.wav   # bare name → resolved in samples/
python main.py /path/to/music/folder --backspin /path/to/my_rewind.wav   # or an explicit path
```

The resolved sample must exist or the app exits at startup.

### Output routing & headphone cueing (PFL)

By default the master mix plays on your system's default output device. You can pin it to a specific device and send a **pre-listen cue** (audition the next track in headphones while the mix keeps playing) to a *second* device — the classic DJ setup:

```bash
python main.py /path/to/music --main-device "Realtek" --headphones-device "B01"
# master mix -> speakers/mixer ;  cue (press L) -> headphones
```

- `--main-device "<name>"` pins the **master** output (a name substring; e.g. your speakers, or a mixer/PA via the built-in codec's AUX jack). Omit to follow the OS default.
- `--headphones-device "<name>"` enables the **cue** and routes it to those headphones. **Must be a separate device — USB-C or Bluetooth**, not the built-in 3.5 mm jack (which shares the speaker codec). Omit to disable cueing.
- `python main.py --list-devices` prints the output devices you can name (do it with any AUX cable already plugged in).

The master and headphones must resolve to **different** devices — the app refuses to start otherwise (including when `--main-device` is omitted and the OS default happens to be the headphones).

On first launch the app analyses every audio file (BPM + key detection). Results are cached in `~/.automix_cache.json`, so subsequent launches are instant.

While a track is playing, the song list shows a **`:)`** next to every track whose tempo is mixable with it — including half-time and double-time matches (e.g. a 64 BPM track is flagged against a 128 BPM track). It's a quick way to spot good next-track candidates.

## Controls

The library browser is a three-level drill-down — **root → subfolders → songs**. Use the arrows to move and drill in/out, or click a folder to open it; only one subfolder stays open at a time.

| Key | Action |
|-----|--------|
| `↑` / `↓` | Move within the current folder level or the song list |
| `→` | Drill in — on the root, reveal its subfolders; on a subfolder, load its songs and jump to the list |
| `←` | Go back up a level — from the song list to the tree, or collapse the current folder |
| `Enter` / click | **Folder:** open it (press again to close; opening one subfolder collapses any other). **Song:** load as **Now Playing** — only while stopped (use `N` + `M` to switch during playback) |
| `N` | Load selected song as **Next Track** |
| `Space` | Play / Pause |
| `S` | Stop |
| `C` | Set cue point on next track (seconds in). The crossfade snaps it to the nearest bar (shown as `Mix:`); the backspin uses the raw value (shown as `Cue:`) |
| `F` | Set fade duration in seconds (default: 16) |
| `R` | Set tempo-restore duration in seconds — how long the stretched track takes to ramp back to its own BPM after the fade (default: 30) |
| `P` | Prepare mix — tempo-matches next track to current BPM (skips stretching when tempos already match) |
| `M` | Mix now — start the crossfade |
| `B` | Backspin transition — stop the current track, play a backspin SFX, then drop the next track in from its cue (see below) |
| `A` | Toggle **auto-mix** — automatically crossfade into the next track as the current one nears its end (see below). Off by default; a magenta `AUTO` marker shows when it's armed |
| `L` | Cue / pre-listen the queued next track in the headphones (needs `--headphones-device`). Press again to stop. See [Output routing & headphone cueing](#output-routing--headphone-cueing-pfl) |
| `[` / `]` | While cueing: seek the cue ∓5 s |
| `,` / `.` | Master volume down / up (0–200%; boosts quiet tracks, safely limited) |
| `9` / `0` | Headphone-cue volume down / up (0–100%) |
| `G` | Toggle the **master FX gate** on / off (applies the selected effect to the live mix) |
| `1` / `2` / `3` | Select the FX effect: **high-pass** / **low-pass** / **Trans** (tempo-synced gate) |
| Mouse wheel | Adjust the selected effect's intensity — **only while the gate is on** (otherwise it scrolls the lists) |
| `Q` | Quit |

## Auto-Mix Workflow

1. Press `Enter` on a track to start playing it.
2. Navigate to the next track and press `N` to queue it.
3. Optionally press `C` to set where in the next track it should start (cue point).
4. Optionally press `F` to change the crossfade duration.
5. Press `P` to prepare the mix (time-stretches the next track in the background).
6. Press `M` when ready to start the crossfade.

Or, instead of steps 4–6, press `B` for a **backspin transition** — a quick, hard cut with a DJ rewind effect (see below). It needs the next track queued (step 2) but **not** prepared.

To hand the whole thing off, press `A` to arm the [auto-mix](#auto-mix-hands-off) — it triggers the transition for you as each track nears its end, and even picks the next song when nothing is queued.

## Smart tempo matching

When you prepare a mix, AutoMix compares the two tracks' tempos (accounting for half-time / double-time relationships) and picks the least intrusive transition:

- **Matching tempos** — if the tracks are already close enough that they'd stay beat-locked through the crossfade, the next track is mixed **as-is**, with no time-stretching. This avoids any stretching artefacts and makes Prepare instant. The same applies to exact half-/double-time pairs (e.g. 128 BPM into 64 BPM), which lock naturally on every other beat.
- **Mismatched tempos** — the next track is time-stretched (via `rubberband`) to match the current BPM for the duration of the fade, then smoothly ramped back to its own tempo afterwards.

The `:)` indicator in the song list flags tempo-compatible tracks (within a DJ-style beatmatching range, octave-folded), so you can spot good next-track candidates at a glance — closely matched ones will mix with little or no stretching.

## Backspin transition

A DJ-style **backspin / rewind** transition, distinct from the beat-matched crossfade. Press `B` and AutoMix abruptly stops the current track, plays a backspin SFX one-shot, then drops the queued next track straight in at its natural tempo — no crossfade, no time-stretching. It's the third transition mode alongside **skip** (no stretch) and **stretch** (`rubberband` rate-ramp).

It needs a track playing and a next track queued (`N`) that is **raw** — not prepared and not being prepared (`B` is rejected if you've pressed `P`). It's also blocked mid-crossfade and during the post-mix tempo restoration. Any other time, `B` reports why it can't run.

The next track starts from its **raw** cue — `0:00` by default, or exactly the value you typed with `C` (unlike the crossfade, which snaps the cue to the nearest bar). The panel shows both: `Cue:` is where a backspin starts, `Mix:` is where a crossfade starts.

The SFX defaults to `samples/top_DJ_Rewind_SFX_10.mp3`; override it with `--backspin` (see [Usage](#usage)). The `samples/` folder ships several rewind / backspin / scratch one-shots in `.wav` / `.mp3` to choose from.

## Auto-mix (hands-off)

Press **`A`** to arm the auto-mix — a magenta `AUTO` marker appears on the Now Playing panel. While it's armed, AutoMix watches the playing track and, as it enters its **final 10 seconds**, automatically brings in the next track with a 10-second crossfade, so the music never runs out. It adapts to what you've set up:

- **Next track queued and prepared** (`N` then `P`) — does the normal **beat-matched** mix, just triggered for you and timed to land on the bar.
- **Next track queued but raw** (or still preparing) — mixes it in **as-is** at its natural tempo (no stretching), from its cue point, dropping any half-finished prepare.
- **Nothing queued** — automatically picks the **next song in the same folder** as the current track (alphabetical order) and mixes it in as-is from the start.

It's **continuous while armed**: once a track is mixed in, AutoMix keeps going and mixes *its* successor when it nears the end — so it can play out a whole folder unattended, like an auto-DJ. It stops automatically at the **last song in a folder** (nothing left to bring in), and you can stop it any time by pressing **`A`** again to disarm. Disarm to deliberately let the final track of a set play out to the end.

Auto-mix sits alongside the manual controls — arm it as a safety net while you line up the next track by hand, or leave it running for a continuous mix.

## Headphone cueing (PFL) & volume

With a `--headphones-device` configured (see [Output routing & headphone cueing](#output-routing--headphone-cueing-pfl)), press `L` to **pre-listen** the queued next track in your headphones while the master mix keeps playing on the speakers — the standard way to find your drop point before bringing a track in. `L` again stops it; while it's playing, `[` and `]` seek the preview ∓5 s. The cue auditions from the track's **raw** cue point and is independent of the mix engine — you can cue whether the master is playing, paused, or stopped. It auto-stops once you mix, backspin, or queue a different track. If the headphones disconnect mid-set, the cue goes silent and the master keeps playing.

Volume is controlled in-app, so it works even when the master is pinned to a non-default device (where the OS volume keys wouldn't reach it):

- `,` / `.` — **master** volume, 0–200%. Above 100% boosts quiet tracks; the output is hard-limited to full scale so the speakers never receive a beyond-full-scale signal (a loud track pushed past 100% distorts rather than getting louder — boost is headroom for quiet material).
- `9` / `0` — **headphone-cue** volume, 0–100%.

## Master FX

Three live performance effects you can apply to the master mix in real time. Press **`G`** to toggle the **FX gate** on or off; while it's on, the selected effect is applied to whatever is playing. Pick the effect with **`1` / `2` / `3`**, and dial its intensity with the **mouse wheel** (the wheel only adjusts FX while the gate is on — otherwise it scrolls the folder/song lists as usual). The Now Playing panel shows the active effect and its level (e.g. `FX HPF 45%`).

| Key | Effect | What the wheel does |
|-----|--------|---------------------|
| `1` | **High-pass filter** | Sweeps the cutoff up (300 Hz → 4 kHz), progressively thinning the bass and body |
| `2` | **Low-pass filter** | Sweeps the cutoff down (10 kHz → 250 Hz), progressively muffling the treble |
| `3` | **Trans** | A hard on/off gate (chopper) locked to the track's tempo; the wheel steps the rate `off → 1/4 → 1/8 → 1/16 → 1/32` note |

Notes:

- **Everything starts at no effect.** A freshly engaged gate is silent until you wheel up; the filters begin at bypass and Trans begins at *off*.
- **Only one effect is active at a time**, and switching to another resets the previous one back to zero — so re-selecting an effect always starts clean (no sudden re-attack at its old level).
- The **Trans** gate follows the live tempo, including the tempo-restore ramp after a stretched mix, so the chop stays in time.
- The gate disengages automatically on **Stop**, and FX apply to the master only — the headphone cue (PFL) stays dry.

## Banner artwork

The top of the UI shows an **AUTOMIX** wordmark (cfonts "block" font) beside a static pixel-art image, both rendered with terminal half-blocks and recoloured into the UI's neon palette, with a live clock ticking in the top-right corner. **Any low-resolution, flat-colour pixel-art image works** — a sprite, an icon, a small character. A sample ships in `assets/`; swap in your own anytime.

The art is baked into `automix/banner_art.py` by a build-time script, so the running app has no extra dependency. To regenerate it you need **Pillow** (numpy is already installed with the app):

```bash
pip install pillow
```

Drop a PNG into `assets/` and run the integrator:

```bash
# Linux / macOS
.venv/bin/python scripts/pixart_image_integrator.py assets/yourart.png --recolor
```
```powershell
# Windows
.venv\Scripts\python.exe scripts\pixart_image_integrator.py assets\yourart.png --recolor
```

The banner picks up the new art automatically and resizes to fit (the portrait is sized to the wordmark's height). To restore the bundled sample, run the same command with `assets/sample.png`.

**Transparency is automatic:** a PNG with an alpha channel is keyed by its alpha (transparent = background); for an opaque image, the background colour is detected from the border. Pass `--bg none` to keep the background, or `--bg "#rrggbb"` to force a colour.

For just the **AUTOMIX wordmark with no portrait**, pass `--no-image`:

```powershell
.venv\Scripts\python.exe scripts\pixart_image_integrator.py --no-image
```

| Flag | Purpose |
|------|---------|
| `--recolor` | Snap every pixel to the neon palette (green / cyan / yellow / magenta at several brightness levels). Omit to keep the image's original colours. |
| `--no-image` | Wordmark only — bake an empty portrait (no image in the banner). |
| `--rows N` | Image height in character rows (default: the wordmark's height). Larger = more detail, but a taller banner. |
| `--bg auto\|none\|#rrggbb` | Background keyed out to transparent. `auto` (default) keys a PNG's alpha channel when present, otherwise reads the image border; `none` keeps it opaque; or give an explicit colour. |
| `--grid N` / `--grid WxH` | Override the automatic sizing with an explicit cell width (height from aspect) or full grid. |
| `--bg-tolerance`, `--hues`, `--levels` | Finer control over background keying and the recolour palette. Run with `--help` for details. |

**Best results** come from flat-colour pixel art with a clear, uniform background — the script downsamples and snaps colours, so photos or highly detailed images become coarse and abstract. Without `--recolor`, an anti-aliased image may have more colours than the script can letter; it'll tell you to add `--recolor` or reduce the size.

## Supported Formats

MP3, FLAC, WAV, OGG, M4A, AAC, Opus, WMA, AIFF, and anything else ffmpeg can decode.

## Credits

- **DJ backspin sample** — by [Neily Hype](https://neilyhype.com/), published as a free download.
- **8x8 pixel-art cat** — by **SpinachChicken** on [OpenGameArt.org](https://opengameart.org/), released under [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) (public domain).

Thanks to their creators.

## License

Copyright (c) 2026 Emanuele Rambaldi.

The source code is released under the **[MIT License](LICENSE)** — free to use, modify,
and distribute (including commercially), as long as you keep the copyright notice.

The bundled audio in `samples/` (DJ rewind / backspin sound effects) and any music in
`music/` are **not** covered by this license and remain the property of their respective
owners; they are included for demonstration only. See the [LICENSE](LICENSE) file for details.
