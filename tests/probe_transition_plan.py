"""Unit tests for automix.transition.plan_transition -- the skip-vs-stretch
decision and octave-folded r_eff. Pure math, no engine/audio needed."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from automix.transition import (
    plan_transition, tempo_compatible, MAX_DRIFT_BEATS, TEMPO_MATCH_TOLERANCE,
)


def approx(a, b, tol=1e-6):
    return abs(a - b) < tol


def test_identical_tempos_skip():
    p = plan_transition(128.0, 128.0, 30.0)
    assert p.skip, "identical tempos must skip the stretch"
    assert approx(p.start_rate, 1.0)
    assert approx(p.matched_bpm, 128.0)
    assert approx(p.drift_beats, 0.0)
    assert p.relation == ""
    print("  PASS identical tempos -> skip, rate 1.0, no drift")


def test_within_budget_skip():
    # 0.4 BPM gap over a 30s fade -> 0.4*30/60 = 0.2 beats <= 0.25 -> skip.
    p = plan_transition(128.0, 128.4, 30.0)
    assert p.skip, f"0.2-beat drift should skip (drift={p.drift_beats:.4f})"
    assert approx(p.drift_beats, 0.4 * 30.0 / 60.0)
    assert approx(p.matched_bpm, 128.0, tol=1e-3)
    print(f"  PASS +0.4 BPM @30s -> skip (drift {p.drift_beats:.3f} beats)")


def test_over_budget_stretch():
    # 2 BPM gap over 30s -> 1.0 beat drift -> must stretch at the raw ratio.
    p = plan_transition(128.0, 130.0, 30.0)
    assert not p.skip, "1-beat drift must stretch"
    assert approx(p.start_rate, 128.0 / 130.0)
    assert approx(p.drift_beats, 2.0 * 30.0 / 60.0)
    assert p.relation == ""
    print(f"  PASS +2 BPM @30s -> stretch at {p.start_rate:.4f} (drift {p.drift_beats:.2f})")


def test_fade_length_flips_decision():
    # Same 2 BPM gap, but a short cut accumulates little drift -> skip.
    short = plan_transition(128.0, 130.0, 5.0)   # 2*5/60 = 0.167 <= 0.25
    long = plan_transition(128.0, 130.0, 30.0)   # 1.0 > 0.25
    assert short.skip and not long.skip, "fade length must gate the decision"
    print("  PASS same gap: short fade skips, long fade stretches")


def test_exact_half_time_skip():
    # 128 over 64: grids lock 2:1, zero drift, no stretch needed.
    p = plan_transition(128.0, 64.0, 60.0)
    assert p.skip, "exact 2:1 must skip regardless of fade length"
    assert approx(p.start_rate, 1.0), "half-time must fold ratio to 1.0 (NOT 2.0)"
    assert approx(p.matched_bpm, 64.0)
    assert approx(p.drift_beats, 0.0)
    assert p.relation == "half-time"
    print("  PASS 128<->64 -> half-time fold, rate 1.0, zero drift")


def test_exact_double_time_skip():
    # 70 under 140: incoming is double-time; effective ratio folds to 1.0.
    p = plan_transition(70.0, 140.0, 60.0)
    assert p.skip
    assert approx(p.start_rate, 1.0)
    assert approx(p.matched_bpm, 140.0)
    assert p.relation == "double-time"
    print("  PASS 70<->140 -> double-time fold, rate 1.0, zero drift")


def test_near_octave_guards_the_bug():
    # 128 over 63: near-but-not-exact octave, over budget at 30s. The stretch
    # rate MUST be the folded ~1.016, NOT the naive 2.032 that would speed the
    # 63 BPM track to 128 and destroy the half-time feel.
    p = plan_transition(128.0, 63.0, 30.0)
    assert not p.skip
    assert approx(p.start_rate, 128.0 / 63.0 / 2.0, tol=1e-4), \
        f"folded rate expected ~1.016, got {p.start_rate:.4f}"
    assert p.start_rate < 1.1, "must NOT stretch at the naive 2.03 ratio (the bug)"
    assert approx(p.matched_bpm, 64.0, tol=0.1)
    assert p.relation == "half-time"
    print(f"  PASS 128<->63 -> stretch at folded {p.start_rate:.4f}, not 2.03 (bug guard)")


def test_dnb_half_time():
    # 174 DnB counted as 87 -> exact octave skip.
    p = plan_transition(174.0, 87.0, 20.0)
    assert p.skip and approx(p.start_rate, 1.0) and p.relation == "half-time"
    print("  PASS 174<->87 -> half-time skip")


def test_zero_fade_always_skips():
    p = plan_transition(128.0, 135.0, 0.0)
    assert p.skip and approx(p.drift_beats, 0.0), "a hard cut has no time to drift"
    print("  PASS fade=0 -> skip (no time to drift)")


def test_missing_tempo_skips_without_dividing():
    for now, nxt in ((128.0, 0.0), (0.0, 128.0), (0.0, 0.0)):
        p = plan_transition(now, nxt, 30.0)
        assert p.skip and approx(p.start_rate, 1.0), \
            f"missing tempo ({now},{nxt}) should skip at rate 1.0"
        assert p.matched_bpm == nxt
    print("  PASS missing BPM -> skip at rate 1.0 (no division)")


def test_drift_formula_matches_spec():
    # drift_beats = (next_bpm/60) * |r_eff-1| * fade ; same-tempo form |dbpm|*fade/60.
    p = plan_transition(124.0, 121.0, 24.0)
    expected = abs(124.0 - 121.0) * 24.0 / 60.0
    assert approx(p.drift_beats, expected, tol=1e-6), \
        f"drift {p.drift_beats} != spec {expected}"
    print(f"  PASS drift formula matches spec ({p.drift_beats:.3f} beats)")


def test_tempo_compatible():
    # Same tempo, exact octave, and double-time all match.
    assert tempo_compatible(128.0, 128.0)
    assert tempo_compatible(128.0, 64.0), "exact half-time should match"
    assert tempo_compatible(70.0, 140.0), "exact double-time should match"
    # Within the 6% window (folded): 128 vs 132 (~3%) matches.
    assert tempo_compatible(128.0, 132.0)
    # Well outside: 128 vs 150 (~17%) and its octaves do not match.
    assert not tempo_compatible(128.0, 150.0)
    # Near-octave just outside tolerance: 128 vs 70 -> folded 128/140=0.914 (~9%).
    assert not tempo_compatible(128.0, 70.0)
    # Unknown tempo never matches.
    assert not tempo_compatible(0.0, 128.0)
    assert not tempo_compatible(128.0, 0.0)
    # Just inside the tolerance matches; just outside does not.
    assert tempo_compatible(128.0, 128.0 / (1.0 + 0.99 * TEMPO_MATCH_TOLERANCE))
    assert not tempo_compatible(128.0, 128.0 / (1.0 + 1.5 * TEMPO_MATCH_TOLERANCE))
    print(f"  PASS tempo_compatible: same/octave/within-{TEMPO_MATCH_TOLERANCE:.0%} match, far misses")


if __name__ == "__main__":
    assert MAX_DRIFT_BEATS == 0.25
    test_identical_tempos_skip()
    test_within_budget_skip()
    test_over_budget_stretch()
    test_fade_length_flips_decision()
    test_exact_half_time_skip()
    test_exact_double_time_skip()
    test_near_octave_guards_the_bug()
    test_dnb_half_time()
    test_zero_fade_always_skips()
    test_missing_tempo_skips_without_dividing()
    test_drift_formula_matches_spec()
    test_tempo_compatible()
    print("\nAll transition-plan tests passed.")
