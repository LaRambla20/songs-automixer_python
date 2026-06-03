"""End-to-end test of the bar-alignment pipeline against real tracks.

What it proves:
  1. analyze_file produces plausible downbeat data for two real tracks.
  2. The cue-snap math (mirroring NextTrackPanel.set_cue) snaps a typed cue
     to the nearest Track B downbeat.
  3. The "next downbeat after engine position" math (mirroring action_mix_now)
     finds Track A's next downbeat > position + 100 ms safety.
  4. AudioEngine.start_mix with that schedule sample actually flips state to
     MIXING at the right sample â€” verified by driving _callback in a loop and
     watching engine.position + state.
"""

import sys
import threading
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))
from automix.analyzer import analyze_file, empty_record
from automix.audio_engine import AudioEngine, State, SAMPLE_RATE, CHANNELS, BLOCK_SIZE


def make_offline_engine():
    """AudioEngine with state set up but no real OutputStream."""
    eng = AudioEngine.__new__(AudioEngine)
    eng.state = State.IDLE
    eng._lock = threading.Lock()
    eng._now_audio = None
    eng._next_audio = None
    eng._position = 0
    eng._mix_pos = 0
    eng._fade_samples = 0
    eng._paused = False
    eng._pending_mix_at = None
    eng._volume = 1.0
    return eng


def snap_cue(seconds, downbeats):
    """Mirrors NextTrackPanel.set_cue snap math."""
    target = max(0.0, seconds)
    if not downbeats:
        return target, False
    target_sample = int(target * SAMPLE_RATE)
    nearest = min(downbeats, key=lambda d: abs(d - target_sample))
    return nearest / SAMPLE_RATE, True


def pick_next_downbeat(position, downbeats):
    """Mirrors action_mix_now's scheduled-start computation."""
    safety = int(0.1 * SAMPLE_RATE)
    future = [d for d in downbeats if d > position + safety]
    return future[0] if future else None


def main():
    root = Path(__file__).parent.parent / "music"
    mp3s = sorted(p for p in root.rglob("*.mp3"))
    if len(mp3s) < 2:
        print("need at least 2 mp3s under ./music")
        sys.exit(1)
    track_a, track_b = mp3s[0], mp3s[1]
    print(f"Track A: {track_a.name}")
    print(f"Track B: {track_b.name}")

    print("\n[1] Analyzing both tracks...")
    rec_a = analyze_file(str(track_a))
    rec_b = analyze_file(str(track_b))
    print(f"  A: {rec_a['bpm']} BPM, {len(rec_a['downbeats'])} downbeats")
    print(f"  B: {rec_b['bpm']} BPM, {len(rec_b['downbeats'])} downbeats")
    assert rec_a["downbeats"], "Track A produced no downbeats"
    assert rec_b["downbeats"], "Track B produced no downbeats"

    # 2. Cue-snap
    typed_cue_sec = 15.0
    snapped_sec, was_snapped = snap_cue(typed_cue_sec, rec_b["downbeats"])
    snap_sample = int(snapped_sec * SAMPLE_RATE)
    print(f"\n[2] Cue snap (typed {typed_cue_sec}s) -> {snapped_sec:.3f}s "
          f"(snapped={was_snapped}, sample={snap_sample})")
    assert was_snapped
    assert snap_sample in rec_b["downbeats"], "snapped value not a real downbeat"
    drift_sec = abs(snapped_sec - typed_cue_sec)
    half_bar = (60 / rec_b["bpm"]) * 2
    if drift_sec > half_bar:
        print(f"  NOTE: snap drifted {drift_sec*1000:.0f} ms â€” typed cue is before "
              f"any detected downbeat (track has long intro / no clear pulse there). "
              f"This is correct but worth surfacing to the user.")
    else:
        print(f"  drift from typed cue: {drift_sec*1000:.0f} ms (within half a bar)")

    # 3. Pick Track A's next downbeat from a mid-bar position
    track_a_pos = (rec_a["downbeats"][2] + rec_a["downbeats"][3]) // 2  # halfway thru bar 3
    next_db = pick_next_downbeat(track_a_pos, rec_a["downbeats"])
    print(f"\n[3] Track A pos={track_a_pos} ({track_a_pos/SAMPLE_RATE:.3f}s, mid-bar) "
          f"-> next downbeat {next_db} ({next_db/SAMPLE_RATE:.3f}s)")
    assert next_db == rec_a["downbeats"][3], "should have picked the 4th downbeat"

    # 4. Drive the engine with synthetic A/B audio. We use synthetic so we can
    # spot the seam: A is a constant +0.5 pulse, B is a constant -0.5 pulse.
    print("\n[4] Driving engine through scheduled-mix transition...")
    duration_samples = next_db + 2 * BLOCK_SIZE + SAMPLE_RATE
    a_audio = np.full((duration_samples, CHANNELS), 0.5, dtype=np.float32)
    b_audio = np.full((duration_samples, CHANNELS), -0.5, dtype=np.float32)

    eng = make_offline_engine()
    eng.play(a_audio)
    eng._position = track_a_pos
    eng.start_mix(b_audio, fade_seconds=2.0, scheduled_start_sample=next_db)
    assert eng.state == State.PLAYING and eng._pending_mix_at == next_db

    # Run callbacks until we cross the trigger sample
    trigger_callback_idx = None
    states_observed = []
    samples_to_run = next_db - track_a_pos + BLOCK_SIZE
    n_callbacks = (samples_to_run // BLOCK_SIZE) + 1
    crossover_sample = None
    for i in range(n_callbacks):
        before_pos = eng._position
        out = np.zeros((BLOCK_SIZE, CHANNELS), dtype=np.float32)
        eng._callback(out, BLOCK_SIZE, None, None)
        states_observed.append(eng.state)
        if trigger_callback_idx is None and eng.state == State.MIXING:
            trigger_callback_idx = i
            # First mixing sample within this callback: where output value
            # first leaves pure A.
            for j in range(BLOCK_SIZE):
                if abs(out[j, 0] - 0.5) > 1e-6:
                    crossover_sample = before_pos + j
                    break

    print(f"  ran {n_callbacks} callbacks; state flipped to MIXING in callback {trigger_callback_idx}")
    print(f"  crossover sample (output diverged from A): {crossover_sample}")
    print(f"  expected trigger sample:                   {next_db}")
    # The first mixing sample has fade_in == 0, so output == A*1 + B*0 == A by
    # construction â€” visible divergence starts at trigger+1. Allow that 1-sample
    # window; anything more is a real bug.
    offset = crossover_sample - next_db
    assert offset in (0, 1), (
        f"trigger off by {offset} samples ({offset/SAMPLE_RATE*1000:.2f} ms)"
    )

    print("\nAll end-to-end alignment checks passed.")


if __name__ == "__main__":
    main()
