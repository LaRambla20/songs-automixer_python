r"""Validate the master FX (HPF / LPF / tempo-synced Trans gate).

Pure-DSP checks on MasterFx plus an AudioEngine integration check via the headless
__new__ harness (no audio device). Run directly:

    .\.venv\Scripts\python.exe tests\probe_fx.py
"""

import sys
import threading
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))
from automix.fx import MasterFx, TRANS_DIVISIONS, FX_INTENSITY_STEP
from automix.audio_engine import AudioEngine, State, SAMPLE_RATE, CHANNELS, BLOCK_SIZE

SR = SAMPLE_RATE


def _run(fx, sig, block=BLOCK_SIZE):
    out = sig.copy()
    for i in range(0, len(out), block):
        fx.process(out[i:i + block], min(block, len(out) - i))
    return out


def _rms(x):
    return float(np.sqrt(np.mean(x ** 2)))


def _sine(freq, n, amp=0.5):
    t = np.arange(n) / SR
    s = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    return np.stack([s, s], axis=1)


def _set_amt(fx, attr, value):
    """Poke an HPF/LPF amount and recompute coefficients — the headless analog of
    the real app's adjust(), which always recomputes when the wheel moves."""
    setattr(fx, attr, value)
    fx._recompute_filter()


def test_hpf_attenuates_bass_passes_treble():
    fx = MasterFx(SR); fx.set_enabled(True); fx.set_type("hpf"); _set_amt(fx, "hpf_amt", 1.0)
    low, high = _sine(60, SR), _sine(8000, SR)
    low_ratio = _rms(_run(fx, low)) / _rms(low)
    fx.reset()
    high_ratio = _rms(_run(fx, high)) / _rms(high)
    assert low_ratio < 0.1, f"HPF should kill 60Hz, ratio={low_ratio}"
    assert high_ratio > 0.9, f"HPF should pass 8kHz, ratio={high_ratio}"
    print(f"  PASS hpf: 60Hz->{low_ratio:.3f}  8kHz->{high_ratio:.3f}")


def test_hpf_responsive_at_mid_intensity():
    """Regression: the HPF must act across its range, not only near 100%. At 50%
    intensity it should already remove a clear chunk of a 60Hz tone."""
    fx = MasterFx(SR); fx.set_enabled(True); fx.set_type("hpf"); _set_amt(fx, "hpf_amt", 0.5)
    low = _sine(60, SR)
    ratio = _rms(_run(fx, low)) / _rms(low)
    assert ratio < 0.5, f"HPF at 50%% barely audible (ratio={ratio}); range/order too gentle"
    # Early onset: at just 20% the cutoff is ~510 Hz, so a 200 Hz body tone is already
    # strongly cut. This pins the raised floor (at the old 100 Hz floor, amt=0.2 -> ~209 Hz
    # cutoff would leave a 200 Hz tone mostly intact).
    fx2 = MasterFx(SR); fx2.set_enabled(True); fx2.set_type("hpf"); _set_amt(fx2, "hpf_amt", 0.2)
    body = _sine(200, SR)
    onset = _rms(_run(fx2, body)) / _rms(body)
    assert onset < 0.2, f"HPF must engage early: 200Hz at amt=0.2 ratio={onset} (floor too low)"
    print(f"  PASS hpf mid-intensity: 60Hz@0.5 -> {ratio:.3f}; 200Hz@0.2 -> {onset:.3f} (early onset)")


def test_lpf_attenuates_treble_passes_bass():
    fx = MasterFx(SR); fx.set_enabled(True); fx.set_type("lpf"); _set_amt(fx, "lpf_amt", 1.0)
    low, high = _sine(60, SR), _sine(8000, SR)
    high_ratio = _rms(_run(fx, high)) / _rms(high)
    fx.reset()
    low_ratio = _rms(_run(fx, low)) / _rms(low)
    assert high_ratio < 0.1, f"LPF should kill 8kHz, ratio={high_ratio}"
    assert low_ratio > 0.9, f"LPF should pass 60Hz, ratio={low_ratio}"
    print(f"  PASS lpf: 8kHz->{high_ratio:.3f}  60Hz->{low_ratio:.3f}")


def test_bypass_when_disabled_or_zero_intensity():
    high = _sine(8000, SR)
    fx = MasterFx(SR); fx.set_enabled(False)
    assert np.array_equal(_run(fx, high), high), "disabled FX must not touch audio"
    # Enabled but intensity 0 = the no-effect end of the sweep -> bypass.
    fx = MasterFx(SR); fx.set_enabled(True); fx.set_type("hpf"); fx.hpf_amt = 0.0
    assert np.array_equal(_run(fx, high), high), "HPF amt=0 must bypass"
    fx.set_type("lpf"); fx.lpf_amt = 0.0
    assert np.array_equal(_run(fx, high), high), "LPF amt=0 must bypass"
    print("  PASS bypass: disabled / zero-intensity leave audio untouched")


def test_defaults_are_no_effect():
    """A freshly enabled gate must leave audio untouched until the wheel is turned —
    all three effects default to bypass/off."""
    sig = _sine(8000, SR)
    bass = _sine(60, SR)
    for fx_type, probe in (("hpf", sig), ("lpf", sig), ("trans", bass)):
        fx = MasterFx(SR); fx.set_enabled(True); fx.set_type(fx_type); fx.set_tempo(120)
        assert np.array_equal(_run(fx, probe), probe), f"default {fx_type} must be no-effect"
    assert MasterFx(SR).describe() == "HPF 0%", "default HPF label"
    fx = MasterFx(SR); fx.set_type("trans")
    assert fx.describe() == "Trans off", "default Trans must read 'off'"
    print("  PASS defaults: HPF/LPF=0%, Trans=off -> no effect until adjusted")


def test_trans_gate_period_and_duty():
    fx = MasterFx(SR); fx.set_enabled(True); fx.set_type("trans")
    fx.set_tempo(120.0); fx.trans_div_idx = TRANS_DIVISIONS.index(16)  # 1/16
    out = _run(fx, np.ones((SR, 2), dtype=np.float32))
    expected_period = SR * 60.0 / 120.0 * 4.0 / 16.0
    # ~half of each period is fully muted (the closed half), so ~50% near-zero.
    frac_muted = float(np.mean(np.abs(out[:, 0]) < 0.01))
    assert 0.4 < frac_muted < 0.6, f"50%% duty expected, got {frac_muted}"
    # The open half reaches full pass-through somewhere.
    assert out[:, 0].max() > 0.95, "open half should reach unity"
    # Detect period from rising edges (where gain crosses ~0.5 upward).
    g = (np.abs(out[:, 0]) > 0.5).astype(int)
    edges = np.where(np.diff(g) == 1)[0]
    measured = float(np.median(np.diff(edges)))
    assert abs(measured - expected_period) < 2.0, (
        f"gate period {measured} != expected {expected_period}"
    )
    assert fx.describe() == "Trans 1/16"
    print(f"  PASS trans: period={measured:.1f} (expected {expected_period:.1f}), duty~{frac_muted:.2f}")


def test_trans_phase_continuous_across_blocks():
    """Block-wise processing must match a single-shot pass (no per-block phase reset)."""
    fx1 = MasterFx(SR); fx1.set_enabled(True); fx1.set_type("trans"); fx1.set_tempo(128); fx1.trans_div_idx = TRANS_DIVISIONS.index(4)
    fx2 = MasterFx(SR); fx2.set_enabled(True); fx2.set_type("trans"); fx2.set_tempo(128); fx2.trans_div_idx = TRANS_DIVISIONS.index(4)
    sig = np.ones((SR, 2), dtype=np.float32)
    blocked = _run(fx1, sig, block=512)
    oneshot = sig.copy(); fx2.process(oneshot, len(oneshot))
    assert np.allclose(blocked, oneshot), "gate must be phase-continuous across blocks"
    print("  PASS trans: phase continuous across 512-frame blocks")


def test_adjust_clamps_and_targets_selected_effect():
    fx = MasterFx(SR)
    fx.set_type("hpf"); fx.hpf_amt = 0.98
    fx.adjust(+1); fx.adjust(+1)
    assert fx.hpf_amt == 1.0, f"hpf amt clamps at 1.0, got {fx.hpf_amt}"
    fx.hpf_amt = 0.02; fx.adjust(-1); fx.adjust(-1)
    assert fx.hpf_amt == 0.0, f"hpf amt clamps at 0.0, got {fx.hpf_amt}"
    # adjust() must touch only the selected effect.
    fx.set_type("lpf"); before = fx.hpf_amt; fx.lpf_amt = 0.5
    fx.adjust(+1)
    assert fx.hpf_amt == before and abs(fx.lpf_amt - (0.5 + FX_INTENSITY_STEP)) < 1e-9
    # Trans steps the division index, clamped to the table (idx 0 = off).
    fx.set_type("trans"); fx.trans_div_idx = 0
    fx.adjust(-1); assert fx.trans_div_idx == 0, "trans clamps at the OFF position"
    for _ in range(10): fx.adjust(+1)
    assert fx.trans_div_idx == len(TRANS_DIVISIONS) - 1
    print("  PASS adjust: clamps, targets selected effect, steps trans division")


def test_describe_labels():
    fx = MasterFx(SR)
    fx.set_type("hpf"); fx.hpf_amt = 0.65; assert fx.describe() == "HPF 65%"
    fx.set_type("lpf"); fx.lpf_amt = 0.30; assert fx.describe() == "LPF 30%"
    fx.set_type("trans"); fx.trans_div_idx = 0; assert fx.describe() == "Trans off"
    fx.trans_div_idx = TRANS_DIVISIONS.index(32); assert fx.describe() == "Trans 1/32"
    print("  PASS describe: HPF/LPF percent + Trans off/division labels")


def _make_engine():
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
    eng._fx = MasterFx(SR)
    return eng


def test_switching_effect_resets_previous_to_zero():
    """Leaving an effect zeroes it, so re-selecting always starts at no-effect — only the
    currently selected effect is ever non-zero."""
    fx = MasterFx(SR); fx.set_type("hpf"); _set_amt(fx, "hpf_amt", 0.6)
    fx.set_type("lpf")
    assert fx.hpf_amt == 0.0, "switching away from HPF must zero it"
    fx.set_type("hpf")
    assert fx.hpf_amt == 0.0 and fx.describe() == "HPF 0%", "re-selecting HPF starts clean"
    # Trans is reset to its OFF index when left.
    fx.set_type("trans"); fx.trans_div_idx = TRANS_DIVISIONS.index(16)
    fx.set_type("lpf")
    assert fx.trans_div_idx == 0, "switching away from Trans must reset it to off"
    print("  PASS switch-reset: leaving an effect zeroes it; only the selected one is non-zero")


def test_coeffs_computed_in_setters_and_state_reset_on_change():
    """#1/#2: butter() runs in the setters (not the callback), and the carried filter
    state is dropped on effect-type change and gate-enable (but kept on intensity steps)."""
    fx = MasterFx(SR); fx.set_type("hpf"); fx.set_enabled(True)
    _set_amt(fx, "hpf_amt", 0.8)
    assert fx._b is not None, "coefficients must be ready before any process() call"
    # Build up some delay state, then a small intensity step keeps it (continuity).
    _run(fx, _sine(120, SR // 4))
    assert fx._zi is not None
    kept = fx._zi
    fx.adjust(+1)                     # small HPF step -> recompute coeffs, keep _zi
    assert fx._zi is kept, "intensity step must preserve filter state for continuity"
    # Switching effect drops the carried state (different filter).
    fx.set_type("lpf")
    assert fx._zi is None, "effect-type change must reset filter state"
    # Re-engaging the gate also starts from rest.
    _set_amt(fx, "lpf_amt", 0.5); _run(fx, _sine(120, SR // 8))
    assert fx._zi is not None
    fx.set_enabled(False); fx.set_enabled(True)
    assert fx._zi is None, "gate re-enable must reset filter state"
    print("  PASS coeffs/state: setters compute butter; reset on type-change & re-enable, kept on step")


def test_engine_applies_fx_and_resets_on_play():
    eng = _make_engine()
    high = _sine(8000, SR)
    eng.play(high)                       # play() must call _fx.reset()
    eng.set_fx_enabled(True)
    eng.set_fx_type("lpf"); _set_amt(eng._fx, "lpf_amt", 1.0)
    out = np.zeros((BLOCK_SIZE, CHANNELS), dtype=np.float32)
    eng._callback(out, BLOCK_SIZE, None, None)
    # The LPF should be visibly attenuating the 8kHz block vs. the unprocessed source.
    raw = high[:BLOCK_SIZE]
    assert _rms(out) < 0.5 * _rms(raw), "engine callback did not apply the LPF"
    # State setters round-trip through fx_state().
    enabled, label = eng.fx_state()
    assert enabled and label == "LPF 100%", f"fx_state={enabled},{label}"
    # reset() fires on play(): fresh track starts the gate phase at 0.
    eng.set_fx_type("trans"); eng.set_fx_tempo(120)
    eng.play(high)
    assert eng._fx._trans_phase == 0, "play() must reset gate phase"
    print("  PASS engine: callback applies FX, fx_state round-trips, play() resets state")


if __name__ == "__main__":
    test_hpf_attenuates_bass_passes_treble()
    test_hpf_responsive_at_mid_intensity()
    test_lpf_attenuates_treble_passes_bass()
    test_bypass_when_disabled_or_zero_intensity()
    test_defaults_are_no_effect()
    test_trans_gate_period_and_duty()
    test_trans_phase_continuous_across_blocks()
    test_adjust_clamps_and_targets_selected_effect()
    test_describe_labels()
    test_switching_effect_resets_previous_to_zero()
    test_coeffs_computed_in_setters_and_state_reset_on_change()
    test_engine_applies_fx_and_resets_on_play()
    print("\nAll FX tests passed.")
