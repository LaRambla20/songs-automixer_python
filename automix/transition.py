"""Tempo-transition planning: decide whether an incoming track needs time-
stretching at all, and at what rate.

Pure math — no rubberband, no engine state — so it can be unit-tested directly
(see tests/probe_transition_plan.py). The app uses `plan_transition()` at
Prepare time to choose between two paths:

  * SKIP    — mix the incoming track at its natural tempo (no rubberband). Valid
              when the beat slip accumulated over the crossfade stays under
              MAX_DRIFT_BEATS. Covers near-identical tempos AND exact 2:1
              (half-/double-time) pairs, where the grids lock with no stretch.
  * STRETCH — render a transition buffer whose constant-rate region plays at
              `start_rate` (= the octave-folded ratio r_eff). This is also the
              fix for the half-/double-time case: the naive ratio now_bpm/next_bpm
              would stretch a 64 BPM track to 128 and destroy the half-time feel,
              whereas r_eff folds that to ~1.0.

Drift model (see CLAUDE.md): with the incoming track played unstretched, the two
grids start beat-aligned on the trigger downbeat and slide apart at the effective
beat-rate mismatch. Total slip at end of fade:

    drift_beats = (next_bpm / 60) * |r_eff - 1| * fade_seconds

For the same-tempo branch (r_eff = now/next) this reduces to the absolute-tempo-
independent form |now_bpm - next_bpm| * fade_seconds / 60.
"""

import math
from dataclasses import dataclass


# Total end-of-fade beat slip we tolerate before insisting on a stretch. A quarter
# beat keeps the loudest mid-fade slip near 1/8 beat — clean for a crossfade.
# Implied skip windows: +/-0.5 BPM @30s fade, +/-1.0 BPM @15s, +/-1.88 BPM @8s;
# exact octaves (128<->64) always skip (drift 0).
MAX_DRIFT_BEATS = 0.25


@dataclass
class TransitionPlan:
    skip: bool          # True -> mix raw audio, no rubberband, no restore ramp
    start_rate: float   # r_eff; fed to make_transition_buffer on the STRETCH path
    matched_bpm: float  # next_bpm * r_eff = incoming's perceived BPM during the fade
    drift_beats: float  # projected end-of-fade slip if mixed unstretched (diagnostic)
    relation: str       # "" | "half-time" | "double-time" (status wording)


def plan_transition(now_bpm: float, next_bpm: float, fade_seconds: float) -> TransitionPlan:
    """Decide skip-vs-stretch and the effective stretch rate for mixing an
    incoming track (`next_bpm`) under an outgoing track (`now_bpm`)."""
    # No tempo info on either side: nothing to match against, and rubberband at
    # rate 1.0 would be a pointless render. Treat as a raw (skip) mix.
    if now_bpm <= 0 or next_bpm <= 0:
        return TransitionPlan(
            skip=True, start_rate=1.0, matched_bpm=next_bpm,
            drift_beats=0.0, relation="",
        )

    r = now_bpm / next_bpm
    # Octave folding, capped at one octave each way: pick the interpretation
    # whose stretch is closest to 1.0 in log space (symmetric for c and 1/c).
    candidates = {"": r, "half-time": r / 2.0, "double-time": 2.0 * r}
    relation = min(candidates, key=lambda k: abs(math.log(candidates[k])))
    r_eff = candidates[relation]

    matched_bpm = next_bpm * r_eff
    drift_beats = (next_bpm / 60.0) * abs(r_eff - 1.0) * fade_seconds
    skip = drift_beats <= MAX_DRIFT_BEATS

    return TransitionPlan(
        skip=skip, start_rate=r_eff, matched_bpm=matched_bpm,
        drift_beats=drift_beats, relation=relation,
    )
