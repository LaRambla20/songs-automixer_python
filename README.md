# AutoMix

A terminal-based music auto-mixer for Linux. Load a music folder, browse tracks, and seamlessly crossfade between songs with automatic tempo synchronisation.

## Requirements

- Python 3.9+
- `ffmpeg` installed on your system

Install ffmpeg:
```bash
# Debian / Ubuntu
sudo apt install ffmpeg

# Fedora
sudo dnf install ffmpeg

# Arch
sudo pacman -S ffmpeg
```

## Installation

```bash
pip install -r requirements.txt
```

## Usage

```bash
python main.py /path/to/music/folder
```

On first launch the app analyses every audio file (BPM + key detection). Results are cached in `~/.automix_cache.json`, so subsequent launches are instant.

## Controls

| Key | Action |
|-----|--------|
| `Tab` / arrows | Navigate folder tree and song list |
| `Enter` | Load selected song as **Now Playing** |
| `N` | Load selected song as **Next Track** |
| `Space` | Play / Pause |
| `S` | Stop |
| `←` / `→` | Seek ±10 seconds |
| `C` | Set cue point on next track (seconds into the track) |
| `F` | Set fade duration in seconds (default: 16) |
| `P` | Prepare mix — time-stretches next track to match current BPM |
| `M` | Mix now — start the crossfade |
| `Q` | Quit |

## Auto-Mix Workflow

1. Press `Enter` on a track to start playing it.
2. Navigate to the next track and press `N` to queue it.
3. Optionally press `C` to set where in the next track the fade-in should start (cue point).
4. Optionally press `F` to change the crossfade duration.
5. Press `P` to prepare the mix (time-stretches the next track in the background).
6. Press `M` when ready to start the crossfade.

## Supported Formats

MP3, FLAC, WAV, OGG, M4A, AAC, Opus, WMA, AIFF, and anything else ffmpeg can decode.
