import os
import json
import hashlib
import numpy as np
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

CACHE_PATH = Path.home() / ".automix_cache.json"
ANALYSIS_SAMPLE_RATE = 22050

SUPPORTED_EXTENSIONS = {
    ".mp3", ".flac", ".wav", ".ogg", ".m4a", ".aac",
    ".opus", ".wma", ".aiff", ".aif", ".mp4", ".webm",
}

# Krumhansl-Kessler key profiles
_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
_NOTES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


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


def analyze_file(path: str) -> Tuple[float, str]:
    """Return (bpm, key) for a single audio file."""
    import librosa
    from pydub import AudioSegment

    seg = AudioSegment.from_file(path)
    seg = seg.set_channels(1).set_frame_rate(ANALYSIS_SAMPLE_RATE)
    y = np.array(seg.get_array_of_samples(), dtype=np.float32) / 32768.0

    tempo, _ = librosa.beat.beat_track(y=y, sr=ANALYSIS_SAMPLE_RATE)
    bpm = float(np.atleast_1d(tempo)[0])

    chroma = librosa.feature.chroma_cqt(y=y, sr=ANALYSIS_SAMPLE_RATE)
    key = _estimate_key(chroma.mean(axis=1))

    return round(bpm, 1), key


def scan_folder(root: str) -> List[str]:
    """Recursively list all supported audio files under root."""
    found = []
    for dirpath, _, filenames in os.walk(root):
        for fname in sorted(filenames):
            if Path(fname).suffix.lower() in SUPPORTED_EXTENSIONS:
                found.append(os.path.join(dirpath, fname))
    return found


def analyze_library(
    root: str,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> Dict[str, Tuple[float, str]]:
    """
    Analyze all audio files under root.  Blocks until complete.
    Returns {abs_path: (bpm, key)}.
    """
    files = scan_folder(root)
    total = len(files)

    cache: dict = {}
    if CACHE_PATH.exists():
        try:
            with open(CACHE_PATH) as f:
                cache = json.load(f)
        except Exception:
            cache = {}

    results: Dict[str, Tuple[float, str]] = {}
    to_analyze: List[Tuple[str, str]] = []

    for path in files:
        k = _file_key(path)
        if k in cache:
            results[path] = (cache[k]["bpm"], cache[k]["key"])
        else:
            to_analyze.append((path, k))

    done = len(results)
    if progress_callback:
        progress_callback(done, total)

    for path, cache_key in to_analyze:
        try:
            bpm, key = analyze_file(path)
        except Exception:
            bpm, key = 0.0, "?"
        results[path] = (bpm, key)
        cache[cache_key] = {"bpm": bpm, "key": key}
        done += 1
        if progress_callback:
            progress_callback(done, total)

    try:
        with open(CACHE_PATH, "w") as f:
            json.dump(cache, f)
    except Exception:
        pass

    return results
