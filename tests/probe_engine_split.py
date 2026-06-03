"""Validate AudioEngine's scheduled-start chunk-split logic without an audio device.

Builds an engine without calling __init__, sets up minimal state, drives the
callback with synthetic audio, and checks the output is sample-accurate at the
trigger point.
"""

import sys
import threading
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))
from automix.audio_engine import AudioEngine, State, SAMPLE_RATE, CHANNELS, BLOCK_SIZE, MAX_GAIN
from automix.fx import MasterFx


def make_engine():
    """Build an AudioEngine without opening the sounddevice stream."""
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
    eng._fx = MasterFx(SAMPLE_RATE)   # disabled by default -> callback passes audio through
    return eng


def test_scheduled_start_inside_chunk():
    """Trigger lands inside the next callback's range â€” output must contain
    pre-trigger samples from track A, then mixed samples after."""
    eng = make_engine()
    # Track A: constant 0.5 across all samples
    a = np.full((SAMPLE_RATE * 5, CHANNELS), 0.5, dtype=np.float32)
    # Track B: constant -0.5 across all samples
    b = np.full((SAMPLE_RATE * 5, CHANNELS), -0.5, dtype=np.float32)

    eng.play(a)
    # Advance position by 1000 samples (simulate prior playback)
    eng._position = 1000
    # Schedule a mix at sample 1500 with a 1-second fade
    eng.start_mix(b, fade_seconds=1.0, scheduled_start_sample=1500)
    assert eng.state == State.PLAYING, "should stay PLAYING before trigger"
    assert eng._pending_mix_at == 1500

    # Drive one callback of BLOCK_SIZE (2048 frames), which spans 1000..3048,
    # so the trigger at 1500 is inside this chunk (500 frames in).
    out = np.zeros((BLOCK_SIZE, CHANNELS), dtype=np.float32)
    eng._callback(out, BLOCK_SIZE, None, None)

    # After: state should be MIXING (the transition happened inside the callback),
    # _pending_mix_at cleared, _position advanced to 1500+1548=3048, _mix_pos=1548.
    assert eng.state == State.MIXING, f"expected MIXING, got {eng.state}"
    assert eng._pending_mix_at is None
    assert eng._position == 3048, f"expected position 3048, got {eng._position}"
    assert eng._mix_pos == 1548, f"expected mix_pos 1548, got {eng._mix_pos}"

    # Pre-trigger 500 samples must be pure A (0.5)
    assert np.allclose(out[:500], 0.5), "pre-trigger samples not equal to track A"
    # First post-trigger sample: mixing math gives fade_in=t0=0, t1=1548/44100=0.0351
    # so first post-trigger sample should be ~A (fade_in tiny). Sanity: should not
    # be silence.
    assert abs(out[500, 0]) > 0.4, f"trigger sample silent: {out[500, 0]}"
    # By the end of the chunk, fade_in has reached 0.0351 â†’ output ~= 0.5*0.965 + (-0.5)*0.0351
    expected_end = 0.5 * (1 - 1548 / SAMPLE_RATE) + (-0.5) * (1548 / SAMPLE_RATE)
    actual_end = float(out[-1, 0])
    assert abs(actual_end - expected_end) < 0.01, (
        f"end-of-chunk sample {actual_end} != expected {expected_end}"
    )
    print(f"  PASS scheduled_start_inside_chunk: split correctly at sample 1500")


def test_master_volume_scales_output():
    """set_volume(0.5) halves the callback output; 1.0 leaves it untouched."""
    eng = make_engine()
    a = np.full((SAMPLE_RATE, CHANNELS), 0.5, dtype=np.float32)
    eng.play(a)
    eng._volume = 0.5
    out = np.zeros((BLOCK_SIZE, CHANNELS), dtype=np.float32)
    eng._callback(out, BLOCK_SIZE, None, None)
    assert np.allclose(out, 0.25), f"gain 0.5 should halve 0.5 -> 0.25, got {out[0,0]}"
    # clamp behaviour: ceiling is MAX_GAIN (2.0), floor 0.0
    eng.set_volume(5.0)
    assert eng.volume == MAX_GAIN, f"should clamp to MAX_GAIN, got {eng.volume}"
    eng.set_volume(-1.0)
    assert eng.volume == 0.0
    print("  PASS master_volume: callback scales by gain, set_volume clamps 0..MAX_GAIN")


def test_master_volume_boost_limits_output():
    """Boost (>1.0) scales quiet material but hard-limits the peak to +-1.0."""
    eng = make_engine()
    eng.play(np.full((SAMPLE_RATE, CHANNELS), 0.4, dtype=np.float32))
    eng._volume = 2.0
    out = np.zeros((BLOCK_SIZE, CHANNELS), dtype=np.float32)
    eng._callback(out, BLOCK_SIZE, None, None)
    assert np.allclose(out, 0.8), f"0.4 * 2.0 should be 0.8 (no clip), got {out[0,0]}"
    # A signal that would exceed full scale is clamped, not amplified past 1.0.
    eng2 = make_engine()
    eng2.play(np.full((SAMPLE_RATE, CHANNELS), 0.8, dtype=np.float32))
    eng2._volume = 2.0
    out2 = np.zeros((BLOCK_SIZE, CHANNELS), dtype=np.float32)
    eng2._callback(out2, BLOCK_SIZE, None, None)
    assert np.allclose(out2, 1.0), f"0.8 * 2.0 must hard-limit to 1.0, got {out2[0,0]}"
    print("  PASS master_volume boost: lifts quiet audio, hard-limits peaks to +-1.0")


def test_immediate_start_unchanged():
    """scheduled_start_sample=None â†’ state flips to MIXING right away (legacy)."""
    eng = make_engine()
    a = np.full((SAMPLE_RATE, CHANNELS), 0.5, dtype=np.float32)
    b = np.full((SAMPLE_RATE, CHANNELS), -0.5, dtype=np.float32)
    eng.play(a)
    eng.start_mix(b, fade_seconds=1.0)
    assert eng.state == State.MIXING
    assert eng._pending_mix_at is None
    print("  PASS immediate_start_unchanged: legacy path still works")


def test_scheduled_in_the_past():
    """If schedule sample is already behind position, fall back to immediate mix."""
    eng = make_engine()
    a = np.full((SAMPLE_RATE, CHANNELS), 0.5, dtype=np.float32)
    b = np.full((SAMPLE_RATE, CHANNELS), -0.5, dtype=np.float32)
    eng.play(a)
    eng._position = 5000
    eng.start_mix(b, fade_seconds=1.0, scheduled_start_sample=2000)
    assert eng.state == State.MIXING, "past-trigger should immediately mix"
    assert eng._pending_mix_at is None
    print("  PASS scheduled_in_the_past: fell back to immediate mix")


def test_no_split_when_trigger_after_chunk():
    """Trigger past the next callback's range â†’ stay PLAYING, no audio change."""
    eng = make_engine()
    a = np.full((SAMPLE_RATE, CHANNELS), 0.5, dtype=np.float32)
    b = np.full((SAMPLE_RATE, CHANNELS), -0.5, dtype=np.float32)
    eng.play(a)
    eng._position = 0
    eng.start_mix(b, fade_seconds=1.0, scheduled_start_sample=10000)
    out = np.zeros((BLOCK_SIZE, CHANNELS), dtype=np.float32)
    eng._callback(out, BLOCK_SIZE, None, None)
    assert eng.state == State.PLAYING
    assert eng._pending_mix_at == 10000
    assert np.allclose(out, 0.5), "should be pure track A"
    print("  PASS no_split_when_trigger_after_chunk: armed but quiet")


if __name__ == "__main__":
    test_immediate_start_unchanged()
    test_scheduled_in_the_past()
    test_no_split_when_trigger_after_chunk()
    test_scheduled_start_inside_chunk()
    test_master_volume_scales_output()
    test_master_volume_boost_limits_output()
    print("\nAll engine tests passed.")
