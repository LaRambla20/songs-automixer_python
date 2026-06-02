# AutoMix

A terminal-based music auto-mixer for Linux, macOS, and Windows. Load a music folder, browse tracks, and seamlessly crossfade between songs with automatic tempo synchronisation.

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

On first launch the app analyses every audio file (BPM + key detection). Results are cached in `~/.automix_cache.json`, so subsequent launches are instant.

While a track is playing, the song list shows a **`:)`** next to every track whose tempo is mixable with it — including half-time and double-time matches (e.g. a 64 BPM track is flagged against a 128 BPM track). It's a quick way to spot good next-track candidates.

## Controls

| Key | Action |
|-----|--------|
| `Tab` / arrows | Navigate folder tree and song list |
| `Enter` | Load selected song as **Now Playing** |
| `N` | Load selected song as **Next Track** |
| `Space` | Play / Pause |
| `S` | Stop |
| `→` | In folder tree: load the folder's songs and jump to the song list |
| `←` | In song list: clear it and return to the folder tree |
| `C` | Set cue point on next track (seconds into the track) |
| `F` | Set fade duration in seconds (default: 16) |
| `P` | Prepare mix — tempo-matches next track to current BPM (skips stretching when tempos already match) |
| `M` | Mix now — start the crossfade |
| `Q` | Quit |

## Auto-Mix Workflow

1. Press `Enter` on a track to start playing it.
2. Navigate to the next track and press `N` to queue it.
3. Optionally press `C` to set where in the next track the fade-in should start (cue point).
4. Optionally press `F` to change the crossfade duration.
5. Press `P` to prepare the mix (time-stretches the next track in the background).
6. Press `M` when ready to start the crossfade.

## Smart tempo matching

When you prepare a mix, AutoMix compares the two tracks' tempos (accounting for half-time / double-time relationships) and picks the least intrusive transition:

- **Matching tempos** — if the tracks are already close enough that they'd stay beat-locked through the crossfade, the next track is mixed **as-is**, with no time-stretching. This avoids any stretching artefacts and makes Prepare instant. The same applies to exact half-/double-time pairs (e.g. 128 BPM into 64 BPM), which lock naturally on every other beat.
- **Mismatched tempos** — the next track is time-stretched (via `rubberband`) to match the current BPM for the duration of the fade, then smoothly ramped back to its own tempo afterwards.

The `:)` indicator in the song list flags tempo-compatible tracks (within a DJ-style beatmatching range, octave-folded), so you can spot good next-track candidates at a glance — closely matched ones will mix with little or no stretching.

## Roadmap

### Backspin / rewind transition (planned)

A future special transition mode — a DJ-style **backspin / rewind** effect layered into the mix, distinct from the current beat-matched crossfade. The `samples/` folder stages the source SFX for it (vinyl rewind, backspin, and scratch one-shots in `.wav` / `.mp3`). This is not wired into the app yet; when built, it will slot in as a third transition mode alongside the existing **skip** (no stretch) and **stretch** (`rubberband` rate-ramp) paths.

## Supported Formats

MP3, FLAC, WAV, OGG, M4A, AAC, Opus, WMA, AIFF, and anything else ffmpeg can decode.
