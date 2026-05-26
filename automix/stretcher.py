"""High-quality time-varying time-stretching via the rubberband CLI.

This module is the *only* place rubberband is invoked.  All audio time-stretching
in the project routes through `make_transition_buffer`.
"""

import os
import re
import shutil
import subprocess
import tempfile
import threading
from typing import Callable, Optional

import numpy as np
import soundfile as sf


RUBBERBAND_BIN = "rubberband"
TIMEMAP_ANCHORS = 64
_PERCENT_RE = re.compile(r"^\s*(\d{1,3})%\s*$")


def rubberband_available() -> bool:
    return shutil.which(RUBBERBAND_BIN) is not None


def _smootherstep_integral(x: float) -> float:
    # antiderivative of s(x) = 6x^5 - 15x^4 + 10x^3, S(0) = 0
    return x**6 - 3 * x**5 + 2.5 * x**4


def _build_timemap(
    total_input_samples: int,
    start_rate: float,
    fade_samples: int,
    restore_samples: int,
) -> list:
    """Return a list of (input_frame, output_frame) anchor pairs.

    Output timeline:
      [0, fade_samples)                          → rate = start_rate (constant)
      [fade_samples, fade_samples+restore)       → rate ramps start_rate → 1.0 via smootherstep
      [fade_samples+restore, ...)                → rate = 1.0 to end

    Note: rubberband implicitly anchors (0, 0). Providing an explicit (0, 0) row
    causes a NaN time-ratio computation at the very first sample and produces wrong
    output, so we omit it and let the first explicit anchor define the start.
    """
    points = []

    in_at_fade_end = int(round(fade_samples * start_rate))
    points.append((in_at_fade_end, fade_samples))

    if restore_samples > 0:
        for i in range(1, TIMEMAP_ANCHORS + 1):
            x = i / TIMEMAP_ANCHORS
            in_consumed_in_ramp = (
                start_rate * x * restore_samples
                + (1.0 - start_rate) * restore_samples * _smootherstep_integral(x)
            )
            in_frame = in_at_fade_end + int(round(in_consumed_in_ramp))
            out_frame = fade_samples + int(round(x * restore_samples))
            points.append((in_frame, out_frame))

    in_at_ramp_end, out_at_ramp_end = points[-1]
    remaining_input = total_input_samples - in_at_ramp_end
    if remaining_input > 0:
        points.append(
            (total_input_samples, out_at_ramp_end + remaining_input)
        )

    # Strictly monotonic in both dimensions, clipped to valid input range,
    # starting strictly after (0, 0) which rubberband anchors implicitly.
    cleaned = []
    last_in, last_out = 0, 0
    for in_f, out_f in points:
        in_f = max(0, min(in_f, total_input_samples))
        if in_f > last_in and out_f > last_out:
            cleaned.append((in_f, out_f))
            last_in, last_out = in_f, out_f
    return cleaned


# Pass-weight calibration: in R3, "Pass 1: Studying" is essentially instant on modern
# hardware (~1% of wall time) and "Pass 2: Processing" does ~99% of the work. A 50/50
# split would make the BPM display snap to halfway inside a single frame, then crawl
# the remainder over many seconds — visibly broken. The 5/95 split reflects reality.
_PASS1_WEIGHT = 0.05
_PASS2_WEIGHT = 0.95


def _drain_stderr_with_progress(stderr, progress_callback: Callable[[float], None]):
    """Read rubberband's stderr character-by-character, parse `Pass N:` markers and
    `<n>%` updates, and forward a 0.0–1.0 fraction to `progress_callback`.

    Rubberband uses CR-overwrite for the progress digits, so line-buffered readline()
    won't see updates promptly — we manually chunk on either CR or LF.
    """
    pass_idx = 0   # 0 = before any pass, 1 = pass 1 active, 2 = pass 2 active
    buf_chars = []
    while True:
        ch = stderr.read(1)
        if ch == "":
            break
        if ch in ("\r", "\n"):
            chunk = "".join(buf_chars)
            buf_chars.clear()
            if not chunk:
                continue
            if "Pass 1:" in chunk:
                pass_idx = 1
                _safe_progress(progress_callback, 0.0)
                continue
            if "Pass 2:" in chunk:
                pass_idx = 2
                _safe_progress(progress_callback, _PASS1_WEIGHT)
                continue
            m = _PERCENT_RE.match(chunk)
            if m and pass_idx in (1, 2):
                pct = min(100, int(m.group(1)))
                if pass_idx == 1:
                    frac = _PASS1_WEIGHT * (pct / 100.0)
                else:
                    frac = _PASS1_WEIGHT + _PASS2_WEIGHT * (pct / 100.0)
                _safe_progress(progress_callback, frac)
        else:
            buf_chars.append(ch)


def _safe_progress(cb: Callable[[float], None], frac: float) -> None:
    try:
        cb(frac)
    except Exception:
        # The UI callback's failure must never kill the stretcher thread.
        pass


def make_transition_buffer(
    audio: np.ndarray,
    start_rate: float,
    fade_seconds: float,
    restore_seconds: float,
    sample_rate: int = 44100,
    progress_callback: Optional[Callable[[float], None]] = None,
) -> np.ndarray:
    """Time-stretch `audio` with a smoothly varying rate, in one rubberband call.

    `audio` is the entire remainder of the incoming track from its cue point,
    shape (N, 2) float32 in [-1.0, 1.0] at `sample_rate`.

    The output:
      * first `fade_seconds` at `start_rate` (matching the outgoing-track BPM)
      * next `restore_seconds` smoothly returning to rate 1.0 via smootherstep
      * remainder of the track at rate 1.0

    If `progress_callback` is given it receives a 0.0–1.0 fraction reflecting
    rubberband's own pass-and-percent progress (Pass 1 → 0–0.5, Pass 2 → 0.5–1.0).
    """
    if audio.ndim != 2 or audio.shape[1] != 2:
        raise ValueError(f"expected stereo (N, 2) audio, got shape {audio.shape}")

    fade_samples = int(round(fade_seconds * sample_rate))
    restore_samples = int(round(restore_seconds * sample_rate))

    timemap = _build_timemap(len(audio), start_rate, fade_samples, restore_samples)
    total_output_samples = timemap[-1][1]
    total_duration_s = total_output_samples / sample_rate

    tmpdir = tempfile.mkdtemp(prefix="automix_rb_")
    in_path = os.path.join(tmpdir, "in.wav")
    out_path = os.path.join(tmpdir, "out.wav")
    map_path = os.path.join(tmpdir, "timemap.txt")
    try:
        sf.write(in_path, audio, sample_rate, subtype="FLOAT")
        # rubberband timemap format: "<input_frame> <output_frame>" (single space, no tab)
        with open(map_path, "w") as f:
            for in_f, out_f in timemap:
                f.write(f"{in_f} {out_f}\n")

        # -3            : R3 (fine) engine — much higher quality than the default R2
        # -M <file>     : time map (key-frame anchors)
        # -D <seconds>  : overall output duration (required alongside --timemap)
        # --centre-focus: better stereo handling on 2-channel material
        cmd = [
            RUBBERBAND_BIN,
            "-3",
            "--centre-focus",
            "-M", map_path,
            "-D", f"{total_duration_s:.6f}",
            in_path,
            out_path,
        ]

        if progress_callback is None:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(
                    f"rubberband failed (exit {result.returncode}): {result.stderr.strip()}"
                )
        else:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            reader = threading.Thread(
                target=_drain_stderr_with_progress,
                args=(proc.stderr, progress_callback),
                daemon=True,
            )
            reader.start()
            proc.stdout.read()    # drain stdout so the pipe doesn't fill
            rc = proc.wait()
            reader.join(timeout=2.0)
            _safe_progress(progress_callback, 1.0)
            if rc != 0:
                raise RuntimeError(f"rubberband failed (exit {rc})")

        out, sr = sf.read(out_path, dtype="float32", always_2d=True)
        if sr != sample_rate:
            raise RuntimeError(f"rubberband returned wrong sample rate {sr}")
        if out.shape[1] == 1:
            out = np.repeat(out, 2, axis=1)
        return out.astype(np.float32)
    finally:
        for p in (in_path, out_path, map_path):
            try:
                os.remove(p)
            except OSError:
                pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass
