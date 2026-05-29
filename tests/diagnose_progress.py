"""Diagnostic for the Prepare-phase BPM oscillation bug.

Runs `make_transition_buffer` on a real audio slice with a ratio matching the
user's reported scenario (112 BPM -> 107 BPM, ratio ~0.9554), but instead of
calling the production stderr drainer it uses an instrumented version that
logs every raw chunk read from rubberband's stderr *plus* the progress
fraction we would have forwarded to the UI.

After it finishes, inspect `progress_log.txt` to see whether rubberband itself
ever emits a smaller percentage after a larger one, or whether our parsing /
mapping is at fault.

Usage:
    python diagnose_progress.py [path-to-audio] [start_rate]

Defaults to the first song under ./music and start_rate=107/112.
"""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import soundfile as sf

# Ensure we can import the project's stretcher.
sys.path.insert(0, str(Path(__file__).parent.parent))

from automix import stretcher
from automix.audio_engine import AudioEngine

LOG_PATH = Path(__file__).parent / "progress_log.txt"


def pick_audio() -> str:
    if len(sys.argv) >= 2:
        return sys.argv[1]
    root = Path(__file__).parent.parent / "music"
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in (".mp3", ".wav", ".flac", ".m4a", ".ogg"):
            return str(p)
    raise SystemExit("No audio file found under ./music; pass one as argv[1].")


def pick_rate() -> float:
    if len(sys.argv) >= 3:
        return float(sys.argv[2])
    return 107.0 / 112.0   # the user's reported scenario


def make_logging_drainer(log_file):
    """Return a replacement for stretcher._drain_stderr_with_progress that
    logs every chunk + computed fraction to `log_file` while still forwarding
    progress to the real callback."""

    PASS1_W = stretcher._PASS1_WEIGHT
    PASS2_W = stretcher._PASS2_WEIGHT
    PERCENT_RE = stretcher._PERCENT_RE

    def drain(stderr, progress_callback):
        pass_idx = 0
        buf_chars = []
        t0 = time.time()

        def emit(frac, source):
            log_file.write(f"{time.time()-t0:7.3f}s  frac={frac:.6f}  ({source})\n")
            log_file.flush()
            try:
                progress_callback(frac)
            except Exception as e:
                log_file.write(f"  callback raised: {e!r}\n")

        while True:
            ch = stderr.read(1)
            if ch == "":
                break
            if ch in ("\r", "\n"):
                chunk = "".join(buf_chars)
                buf_chars.clear()
                term = "CR" if ch == "\r" else "LF"
                log_file.write(f"{time.time()-t0:7.3f}s  raw[{term}]={chunk!r}\n")
                log_file.flush()
                if not chunk:
                    continue
                if "Pass 1:" in chunk:
                    pass_idx = 1
                    emit(0.0, "Pass1 marker")
                    continue
                if "Pass 2:" in chunk:
                    pass_idx = 2
                    emit(PASS1_W, "Pass2 marker")
                    continue
                m = PERCENT_RE.match(chunk)
                if m and pass_idx in (1, 2):
                    pct = min(100, int(m.group(1)))
                    if pass_idx == 1:
                        frac = PASS1_W * (pct / 100.0)
                    else:
                        frac = PASS1_W + PASS2_W * (pct / 100.0)
                    emit(frac, f"pass{pass_idx} {pct}%")
            else:
                buf_chars.append(ch)

    return drain


def main():
    audio_path = pick_audio()
    start_rate = pick_rate()
    print(f"Audio:      {audio_path}")
    print(f"start_rate: {start_rate:.6f}")
    print(f"Log file:   {LOG_PATH}")

    eng = AudioEngine()
    try:
        audio = eng.load_audio(audio_path)
    finally:
        eng.close()

    # Skip the first ~20s to approximate a typical cue offset.
    cue_samples = min(20 * 44100, max(0, len(audio) - 44100 * 30))
    slice_ = audio[cue_samples:]
    print(f"Audio total: {len(audio)/44100:.1f}s, slice from cue: {len(slice_)/44100:.1f}s")

    received = []  # raw frac values from the stretcher (pre-clamp)
    clamped = []   # frac values that _prep_progress would take in app.py (post-clamp)
    state = {"prep_progress": 0.0}
    def on_progress(p):
        received.append(p)
        # Mirror the production _on_progress clamp from app.py:460.
        p_capped = min(1.0, p)
        if p_capped > state["prep_progress"]:
            state["prep_progress"] = p_capped
        clamped.append(state["prep_progress"])

    with open(LOG_PATH, "w", encoding="utf-8") as log_file:
        log_file.write(f"# audio={audio_path}\n# start_rate={start_rate}\n\n")
        original_drain = stretcher._drain_stderr_with_progress
        stretcher._drain_stderr_with_progress = make_logging_drainer(log_file)
        try:
            t0 = time.time()
            out = stretcher.make_transition_buffer(
                slice_,
                start_rate=start_rate,
                fade_seconds=8.0,
                restore_seconds=12.0,
                sample_rate=44100,
                progress_callback=on_progress,
            )
            wall = time.time() - t0
        finally:
            stretcher._drain_stderr_with_progress = original_drain

    print(f"Done in {wall:.1f}s, output {len(out)/44100:.1f}s")
    print(f"Received {len(received)} progress updates")

    def count_regressions(seq):
        regs = []
        last = -1.0
        for i, v in enumerate(seq):
            if v + 1e-9 < last:
                regs.append((i, last, v))
            last = max(last, v)
        return regs

    raw_regs = count_regressions(received)
    clamped_regs = count_regressions(clamped)

    print(f"\nRaw callback frac:    {len(raw_regs)} non-monotonic step(s)")
    print(f"Post-clamp frac (UI): {len(clamped_regs)} non-monotonic step(s)")

    if clamped_regs:
        print("  FAIL â€” clamp did not eliminate oscillation:")
        for i, prev, cur in clamped_regs[:10]:
            print(f"  step {i}: {prev:.6f} -> {cur:.6f}")
    else:
        # Show the climb pattern: where progress actually advanced (high water marks).
        print("  PASS â€” UI-visible progress strictly monotonic.")
        prev = -1.0
        jumps = []
        for i, v in enumerate(clamped):
            if v > prev:
                jumps.append((i, prev, v))
                prev = v
        print(f"  {len(jumps)} forward steps total; high-water timeline:")
        # Sample 12 evenly-spaced jumps so we see the shape.
        if len(jumps) > 12:
            stride = len(jumps) // 12
            jumps_sampled = jumps[::stride]
        else:
            jumps_sampled = jumps
        for i, prev_v, cur in jumps_sampled:
            print(f"    step {i:4d}: {prev_v:.4f} -> {cur:.4f}  (delta {cur-prev_v:+.4f})")

    print(f"\nFull stderr trace written to: {LOG_PATH}")


if __name__ == "__main__":
    main()
