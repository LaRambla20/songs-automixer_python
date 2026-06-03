"""Master performance FX applied to the live output in the audio callback.

A single `MasterFx` instance is owned by `AudioEngine` and called from its real-time
`_callback`. Three effects are selectable (only one active at a time):

- ``hpf`` — high-pass filter; intensity sweeps the cutoff up (removes more bass).
- ``lpf`` — low-pass filter; intensity sweeps the cutoff down (removes more treble).
- ``trans`` — a hard on/off gate (chopper) synced to the now-playing BPM; intensity
  steps the beat division off -> 1/4 -> 1/8 -> 1/16 -> 1/32.

All three default to "no effect" (HPF/LPF amount 0 = bypass; Trans division 0 = off);
turning the mouse wheel up engages them.

Filter delay state and the gate phase persist between callback blocks, so the engine
must call `process()` once per block on the contiguous output. All mutation happens via
the engine under its `_lock` (the callback reads under the same lock).
"""

import numpy as np
from scipy.signal import butter, lfilter

# Cutoff sweep endpoints (Hz). intensity 0 is bypassed entirely (handled in
# _process_filter); the lowest non-zero intensity lands at the MIN, intensity 1 at the
# MAX-ward end. The band is kept inside the audible/musical range so the whole knob
# travel is perceptible (a sweep starting at 20 Hz wastes its lower half in the
# sub-bass, which is why the HPF used to seem to "kick in" only near 100%).
HPF_MIN_HZ = 300.0     # first non-zero step already in the low-mids, so the cut is
                       # audible in the first fifth of the wheel travel (a lower floor
                       # wastes the bottom of the knob in the inaudible sub-bass)
HPF_MAX_HZ = 4000.0    # intensity 1: bass + low-mids gone (telephone-thin)
LPF_MIN_HZ = 250.0     # intensity 1: only the low end left (sweeps DOWN to here)
LPF_MAX_HZ = 10000.0   # low intensity: just the air taken off the top

# Wheel step for the HPF/LPF intensity (fraction of the 0..1 range).
FX_INTENSITY_STEP = 0.05

# Beat divisions the Trans gate steps through (denominator of a 4/4 bar note value).
# Index 0 (division 0) is the OFF / bypass position, so the gate defaults to no effect.
TRANS_DIVISIONS = [0, 4, 8, 16, 32]

# Edge ramp applied at each gate open/close to avoid clicks.
_TRANS_RAMP_MS = 2.0

# 4th-order Butterworth (24 dB/oct) — a more decisive, DJ-style filter sweep that is
# audible across the whole intensity range rather than only at the extreme.
_FILTER_ORDER = 4


class MasterFx:
    def __init__(self, sr: int):
        self.sr = sr
        self.enabled = False
        self.fx_type = "hpf"

        # Per-effect intensity, all defaulting to "no effect". HPF/LPF in [0, 1] (0 =
        # bypass); Trans is an index into TRANS_DIVISIONS (0 = off).
        self.hpf_amt = 0.0
        self.lpf_amt = 0.0
        self.trans_div_idx = 0   # default off

        self.bpm = 0.0

        # Biquad coefficients (`_b`/`_a`) are recomputed by `_recompute_filter()` in the
        # param setters (UI thread, under the engine lock) — NEVER in the audio callback,
        # which only runs `lfilter`. `None` means the filter is bypassed (amount 0 or a
        # non-filter effect). `_zi` is the delay state carried between blocks; its shape
        # tracks `_FILTER_ORDER`. Order is fixed so `_zi` stays valid across intensity
        # changes — carried for continuity on small wheel steps, but reset on effect-type
        # change / gate-enable (where the carried state belongs to a different filter or
        # a stale moment and would otherwise click).
        self._b = None
        self._a = None
        self._zi = None
        self._recompute_filter()

        self._trans_phase = 0  # sample counter for the gate, wraps on a period multiple

    # ------------------------------------------------------------------
    # Parameter mutation (called by AudioEngine under its lock)
    # ------------------------------------------------------------------

    def set_enabled(self, on: bool) -> None:
        on = bool(on)
        # Fresh engage (off -> on): start the filter / gate from rest so we don't
        # resume stale delay state captured the last time the gate was active.
        if on and not self.enabled:
            self._zi = None
            self._trans_phase = 0
        self.enabled = on

    def set_type(self, fx_type: str) -> None:
        if fx_type in ("hpf", "lpf", "trans") and fx_type != self.fx_type:
            # Leaving an effect resets it to no-effect, so re-selecting any effect always
            # starts clean (no silent re-attack at its previous intensity). Only the
            # currently selected effect is ever non-zero.
            if self.fx_type == "hpf":
                self.hpf_amt = 0.0
            elif self.fx_type == "lpf":
                self.lpf_amt = 0.0
            else:
                self.trans_div_idx = 0
            self.fx_type = fx_type
            # Switching effect: the carried filter memory / gate phase belongs to the
            # old effect, so drop it (else it transients into the new one).
            self._zi = None
            self._trans_phase = 0
            self._recompute_filter()

    def set_tempo(self, bpm: float) -> None:
        self.bpm = max(0.0, float(bpm))

    def adjust(self, direction: int) -> None:
        """Nudge the SELECTED effect's intensity. direction > 0 = more intense.
        Recomputes filter coefficients here (UI thread) but keeps `_zi` for continuity —
        a small cutoff step is the one case where carrying the state is the cleanest."""
        step = 1 if direction > 0 else -1
        if self.fx_type == "hpf":
            self.hpf_amt = min(1.0, max(0.0, self.hpf_amt + step * FX_INTENSITY_STEP))
            self._recompute_filter()
        elif self.fx_type == "lpf":
            self.lpf_amt = min(1.0, max(0.0, self.lpf_amt + step * FX_INTENSITY_STEP))
            self._recompute_filter()
        else:  # trans: step the division ladder (off -> 1/4 -> ... -> 1/32)
            self.trans_div_idx = min(
                len(TRANS_DIVISIONS) - 1, max(0, self.trans_div_idx + step)
            )

    def reset(self) -> None:
        """Drop filter delay state + gate phase (called on track (re)start) so a new
        track doesn't inherit the previous one's filter ringing or gate offset."""
        self._zi = None
        self._trans_phase = 0

    def describe(self) -> str:
        if self.fx_type == "hpf":
            return f"HPF {round(self.hpf_amt * 100)}%"
        if self.fx_type == "lpf":
            return f"LPF {round(self.lpf_amt * 100)}%"
        div = TRANS_DIVISIONS[self.trans_div_idx]
        return "Trans off" if div <= 0 else f"Trans 1/{div}"

    # ------------------------------------------------------------------
    # Processing (real-time thread)
    # ------------------------------------------------------------------

    def process(self, out: np.ndarray, frames: int) -> None:
        """Apply the active effect to `out` (shape (frames, 2)) in place."""
        if not self.enabled:
            return
        if self.fx_type == "trans":
            self._process_trans(out, frames)
        else:
            self._process_filter(out, frames)

    def _recompute_filter(self) -> None:
        """Compute biquad coefficients for the current filter type + amount. Called from
        the param setters (UI thread, under the engine lock) so `butter()` never runs on
        the real-time audio thread. Sets `_b`/`_a` to None when bypassed (amount 0, or a
        non-filter effect). Does NOT touch `_zi` — continuity across small steps is the
        callers' concern (`set_type`/`set_enabled` reset it; `adjust` keeps it)."""
        if self.fx_type == "hpf":
            amt, btype, lo, hi = self.hpf_amt, "highpass", HPF_MIN_HZ, HPF_MAX_HZ
        elif self.fx_type == "lpf":
            amt, btype, lo, hi = self.lpf_amt, "lowpass", LPF_MAX_HZ, LPF_MIN_HZ
        else:
            self._b = self._a = None
            return
        if amt <= 0.0:
            self._b = self._a = None  # bypass at the "no effect" end of the sweep
            return
        # Log sweep from `lo` (lowest amount) to `hi` (amount 1). For the LPF, lo>hi so it
        # sweeps down. Bypass already handled, so amt is strictly > 0 here.
        cutoff = lo * (hi / lo) ** amt
        nyq = self.sr * 0.5
        wn = min(0.999, max(1e-4, cutoff / nyq))
        self._b, self._a = butter(_FILTER_ORDER, wn, btype=btype)

    def _process_filter(self, out: np.ndarray, frames: int) -> None:
        if self._b is None:
            return  # bypassed (amount 0, or the active effect isn't a filter)
        # Start the filter at rest (zero delay state) on first use / after a reset.
        # Seeding with the unit-step steady state (lfilter_zi) would inject a large
        # spurious transient when the audio starts near zero.
        if self._zi is None:
            self._zi = np.zeros((_FILTER_ORDER, out.shape[1]))
        # Run the recursion in float64 — a low-cutoff biquad in float32 builds up an
        # audible quantisation noise floor; the cast is negligible for one block.
        y, self._zi = lfilter(self._b, self._a, out.astype(np.float64), axis=0, zi=self._zi)
        out[:] = y.astype(np.float32)

    def _process_trans(self, out: np.ndarray, frames: int) -> None:
        div = TRANS_DIVISIONS[self.trans_div_idx]
        if div <= 0 or self.bpm <= 0.0:
            return  # off, or no tempo known -> nothing to do
        beat = self.sr * 60.0 / self.bpm
        period = max(2.0, beat * 4.0 / div)

        n = np.arange(self._trans_phase, self._trans_phase + frames, dtype=np.float64)
        pos = np.mod(n, period)                 # position within the current gate period

        # First half of each period is open, second half muted, with short linear
        # declick ramps at the open edge (rising) and the close edge (falling).
        ramp = max(1.0, self.sr * _TRANS_RAMP_MS / 1000.0)
        rising = np.clip(pos / ramp, 0.0, 1.0)
        falling = np.clip((period * 0.5 - pos) / ramp, 0.0, 1.0)
        gain = np.where(pos < period * 0.5, np.minimum(rising, falling), 0.0).astype(np.float32)

        out *= gain[:, None]
        # Wrap the phase on a whole number of periods to avoid float growth over time.
        self._trans_phase = int((self._trans_phase + frames) % (period * 64))
