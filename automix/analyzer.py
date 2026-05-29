import os
import json
import hashlib
import numpy as np
from pathlib import Path
from typing import Callable, Dict, List, Optional

CACHE_PATH = Path.home() / ".automix_cache.json"
CACHE_VERSION = 2
ANALYSIS_SAMPLE_RATE = 22050   # for librosa decoding & beat tracking
ENGINE_SAMPLE_RATE = 44100     # what beats[] / downbeats[] are indexed in
ANALYSIS_HOP = 512

SUPPORTED_EXTENSIONS = {
    ".mp3", ".flac", ".wav", ".ogg", ".m4a", ".aac",
    ".opus", ".wma", ".aiff", ".aif", ".mp4", ".webm",
}

# Krumhansl-Kessler key profiles
_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
_NOTES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def empty_record() -> Dict:
    """Default per-track record used when a file fails to analyze or isn't cached."""
    return {"bpm": 0.0, "key": "?", "beats": [], "downbeats": []}


def _file_key(path: str) -> str:
    stat = os.stat(path)
    return hashlib.md5(f"{path}:{stat.st_mtime}:{stat.st_size}".encode()).hexdigest()


def _estimate_key(chroma_mean: np.ndarray) -> str:
    best_r, best_label = -np.inf, "C maj"
    for i in range(12):
        r_maj = float(np.corrcoef(chroma_mean, np.roll(_MAJOR, i))[0, 1])
        r_min = float(np.corrcoef(chroma_mean, np.roll(_MINOR, i))[0, 1])
        if r_maj > best_r:
            best_r, best_label = r_maj, f"{_NOTES[i]} maj"
        if r_min > best_r:
            best_r, best_label = r_min, f"{_NOTES[i]} min"
    return best_label


def _pick_downbeat_phase(
    beat_frames: np.ndarray,
    stft_mag: np.ndarray,
    freqs: np.ndarray,
    sr: int,
) -> int:
    """Return phase in {0,1,2,3} such that beats[phase::4] best aligns with kick hits.

    Score each candidate phase by the total low-frequency (40–150 Hz) STFT energy
    in a ±75 ms window around each beat assigned to that phase. The phase with the
    highest score is taken to be the downbeat position. Assumes 4/4 time.
    """
    if len(beat_frames) < 4:
        return 0

    low_band = (freqs >= 40.0) & (freqs <= 150.0)
    if not np.any(low_band):
        return 0
    low_energy = stft_mag[low_band, :].sum(axis=0)

    window_frames = max(1, int(round(0.075 * sr / ANALYSIS_HOP)))
    n_frames = low_energy.shape[0]

    beat_energies = np.zeros(len(beat_frames), dtype=np.float64)
    for i, bf in enumerate(beat_frames):
        lo = max(0, int(bf) - window_frames)
        hi = min(n_frames, int(bf) + window_frames + 1)
        if hi > lo:
            beat_energies[i] = float(low_energy[lo:hi].mean())

    best_phase, best_score = 0, -1.0
    for phase in range(4):
        score = float(beat_energies[phase::4].sum())
        if score > best_score:
            best_phase, best_score = phase, score
    return best_phase


def analyze_file(path: str) -> Dict:
    """Return {bpm, key, beats, downbeats} for a single audio file.

    `beats` and `downbeats` are integer sample indices at ENGINE_SAMPLE_RATE
    (44100 Hz) so the app can use them directly against engine.position without
    any unit conversion.
    """
    import librosa
    from pydub import AudioSegment

    seg = AudioSegment.from_file(path)
    seg = seg.set_channels(1).set_frame_rate(ANALYSIS_SAMPLE_RATE)
    y = np.array(seg.get_array_of_samples(), dtype=np.float32) / 32768.0

    tempo, beat_frames = librosa.beat.beat_track(
        y=y, sr=ANALYSIS_SAMPLE_RATE, hop_length=ANALYSIS_HOP
    )
    bpm = float(np.atleast_1d(tempo)[0])

    chroma = librosa.feature.chroma_cqt(y=y, sr=ANALYSIS_SAMPLE_RATE)
    key = _estimate_key(chroma.mean(axis=1))

    if len(beat_frames) > 0:
        # Convert beats to engine-rate sample indices.
        beat_times = librosa.frames_to_time(
            beat_frames, sr=ANALYSIS_SAMPLE_RATE, hop_length=ANALYSIS_HOP
        )
        beats = (beat_times * ENGINE_SAMPLE_RATE).round().astype(int).tolist()

        # Heuristic downbeat detection on a single STFT pass.
        stft_mag = np.abs(librosa.stft(
            y, n_fft=2048, hop_length=ANALYSIS_HOP
        ))
        freqs = librosa.fft_frequencies(sr=ANALYSIS_SAMPLE_RATE, n_fft=2048)
        phase = _pick_downbeat_phase(beat_frames, stft_mag, freqs, ANALYSIS_SAMPLE_RATE)
        downbeats = beats[phase::4]
    else:
        beats = []
        downbeats = []

    return {
        "bpm": round(bpm, 1),
        "key": key,
        "beats": beats,
        "downbeats": downbeats,
    }


def scan_folder(root: str) -> List[str]:
    """Recursively list all supported audio files under root."""
    found = []
    for dirpath, _, filenames in os.walk(root):
        for fname in sorted(filenames):
            if Path(fname).suffix.lower() in SUPPORTED_EXTENSIONS:
                found.append(os.path.join(dirpath, fname))
    return found


def _load_cache() -> Dict[str, Dict]:
    """Return the `entries` dict from disk. Empty if file missing, malformed, or
    from an older schema version."""
    if not CACHE_PATH.exists():
        return {}
    try:
        with open(CACHE_PATH) as f:
            raw = json.load(f)
    except Exception:
        return {}
    if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION:
        return {}
    entries = raw.get("entries", {})
    return entries if isinstance(entries, dict) else {}


def _save_cache(entries: Dict[str, Dict]) -> None:
    try:
        with open(CACHE_PATH, "w") as f:
            json.dump({"version": CACHE_VERSION, "entries": entries}, f)
    except Exception:
        pass


def _entry_is_valid(entry: Dict) -> bool:
    """A cached entry must have all v2 fields. Older partials get re-analyzed."""
    return (
        isinstance(entry, dict)
        and "bpm" in entry
        and "key" in entry
        and "beats" in entry
        and "downbeats" in entry
    )


def analyze_library(
    root: str,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> Dict[str, Dict]:
    """Analyze all audio files under root. Blocks until complete.
    Returns {abs_path: {bpm, key, beats, downbeats}}.
    """
    files = scan_folder(root)
    total = len(files)

    cache = _load_cache()
    results: Dict[str, Dict] = {}
    to_analyze: List[tuple] = []

    for path in files:
        k = _file_key(path)
        entry = cache.get(k)
        if entry is not None and _entry_is_valid(entry):
            results[path] = entry
        else:
            to_analyze.append((path, k))

    done = len(results)
    if progress_callback:
        progress_callback(done, total)

    for path, cache_key in to_analyze:
        try:
            record = analyze_file(path)
        except Exception:
            record = empty_record()
        results[path] = record
        cache[cache_key] = record
        done += 1
        if progress_callback:
            progress_callback(done, total)

    _save_cache(cache)
    return results
