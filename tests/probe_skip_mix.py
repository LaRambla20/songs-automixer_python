"""Headless tests for the skip-the-stretch mix path: a skip plan must store the
raw cue audio (no rubberband) and arm NO restoration ramp, while a stretch plan
must arm the ramp from the octave-folded matched_bpm. Driven with a stubbed
engine + fake panels, no audio device or Textual mount."""

import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))
from automix.app import AutoMixApp
from automix.audio_engine import State
from automix.transition import TransitionPlan, plan_transition


class FakeEngine:
    def __init__(self):
        self.state = State.PLAYING
        self.paused = False
        self.position = 0
        self.duration = 0
        self.start_mix_calls = []
        self._audio = np.zeros((44100 * 4, 2), dtype=np.float32)
    def start_mix(self, buf, fade, scheduled_start_sample=None):
        self.start_mix_calls.append((buf, fade, scheduled_start_sample))
    def load_audio(self, path):
        return self._audio


class FakeNextPanel:
    def __init__(self, cue=0.0, fade=15.0, restore=20.0, cue_snapped=True):
        self.cue = cue
        self.fade = fade
        self.restore = restore
        self.cue_snapped = cue_snapped
        self.calls = []
    def clear(self): self.calls.append(("clear",))
    def set_status(self, s): self.calls.append(("set_status", s))
    def set_display_bpm(self, b): self.calls.append(("set_display_bpm", b))


class FakeNowPanel:
    def __init__(self): self.calls = []
    def set_track(self, *a, **k): self.calls.append(("set_track", a))
    def clear_mix_from(self): self.calls.append(("clear_mix_from",))
    def clear(self): self.calls.append(("clear",))


def build_app(plan, cue_snapped=True, now_downbeats=None, next_downbeats=None):
    app = AutoMixApp.__new__(AutoMixApp)
    app.engine = FakeEngine()
    app._now_path = "/old.mp3"
    app._now_bpm = 128.0
    app._now_key = "A min"
    app._now_downbeats = now_downbeats if now_downbeats is not None else [44100, 88200]
    app._next_path = "/new.mp3"
    app._next_bpm = 128.4 if plan is None else (plan.matched_bpm / plan.start_rate if plan.start_rate else 128.0)
    app._next_key = "C maj"
    app._next_downbeats = next_downbeats if next_downbeats is not None else [0, 44100]
    app._next_prepared = np.zeros((100, 2), dtype=np.float32)
    app._next_plan = plan
    app._preparing = False
    app._prep_animating = False
    app._prep_progress = 0.0
    app._prep_epoch = 0
    app._cue = None
    app._cue_epoch = 0
    app._cue_loading = False
    app._mix_scheduled = False
    app._pending_now_swap = None
    app._restore_from_bpm = 0.0
    app._restore_to_bpm = 0.0
    app._restore_seconds = 30.0
    app._t_restore_start = 0.0
    app._input_mode = ""
    app._input_buf = ""
    app._songs_in_view = []   # _refresh_match_markers iterates this (no-op when empty)

    next_panel = FakeNextPanel(cue_snapped=cue_snapped)
    now_panel = FakeNowPanel()
    app._next_panel = next_panel
    app._now_panel = now_panel
    app._statuses = []
    app._status = lambda m: app._statuses.append(m)
    app.call_from_thread = lambda fn, *a: fn(*a)
    def fake_query_one(selector, _cls=None):
        return now_panel if "now-playing" in selector else next_panel
    app.query_one = fake_query_one
    return app


def test_skip_mix_arms_no_restore_ramp():
    plan = plan_transition(128.0, 64.0, 30.0)   # exact half-time -> skip
    assert plan.skip
    app = build_app(plan)
    AutoMixApp.action_mix_now(app)

    assert app._restore_from_bpm == 0.0, "skip mix armed a restore-from BPM"
    assert app._restore_to_bpm == 0.0, "skip mix armed a restore-to BPM"
    assert app._t_restore_start == 0.0
    assert len(app.engine.start_mix_calls) == 1, "engine.start_mix not called"
    assert app._next_plan is None, "plan not reset after consumption"
    assert app._next_prepared is None
    # Bar-aligned (deferred): swap held, status mentions no-stretch.
    assert app._mix_scheduled is True
    assert app._pending_now_swap is not None
    assert "no stretch" in app._statuses[-1]
    print("  PASS skip mix: no restore ramp armed, deferred swap, raw buffer mixed")


def test_stretch_mix_arms_ramp_from_matched_bpm():
    plan = plan_transition(128.0, 130.0, 30.0)   # 1-beat drift -> stretch
    assert not plan.skip
    app = build_app(plan)
    AutoMixApp.action_mix_now(app)

    assert abs(app._restore_from_bpm - plan.matched_bpm) < 1e-6, \
        f"ramp should start at matched_bpm {plan.matched_bpm}, got {app._restore_from_bpm}"
    assert abs(app._restore_to_bpm - app._next_bpm) < 1e-6
    assert app._t_restore_start == 0.0, "ramp arms later (MIXING->PLAYING), not now"
    assert app._next_plan is None
    print(f"  PASS stretch mix: ramp armed {app._restore_from_bpm:.1f}->{app._restore_to_bpm:.1f} BPM")


def test_skip_mix_immediate_when_unaligned():
    # No downbeats / cue not snapped -> immediate (non-deferred) mix, swap now.
    plan = plan_transition(128.0, 128.0, 30.0)
    app = build_app(plan, cue_snapped=False, now_downbeats=[], next_downbeats=[])
    AutoMixApp.action_mix_now(app)

    assert app._mix_scheduled is False, "unaligned mix should not defer"
    assert app._now_path == "/new.mp3", "immediate mix did not swap now-playing"
    assert app._restore_from_bpm == 0.0 and app._restore_to_bpm == 0.0
    assert "no stretch" in app._statuses[-1]
    assert app.engine.start_mix_calls[0][2] is None, "scheduled sample should be None"
    print("  PASS skip mix (unaligned): immediate swap, no ramp, no stretch in status")


def test_prepare_skip_stores_raw_audio():
    # Integration: a same-tempo pairing should take the skip path in
    # action_prepare_mix and store the raw cue slice (no rubberband).
    plan = plan_transition(128.0, 128.0, 15.0)
    app = build_app(plan)
    app._next_prepared = None
    app._next_plan = None
    app._next_panel.cue = 1.0   # 1s cue
    app._next_panel.fade = 15.0

    AutoMixApp.action_prepare_mix(app)

    # Worker runs in a daemon thread; poll briefly for completion.
    for _ in range(100):
        if app._next_prepared is not None:
            break
        time.sleep(0.01)

    assert app._next_prepared is not None, "skip prepare did not store a buffer"
    expected_len = len(app.engine._audio) - int(1.0 * 44100)
    assert len(app._next_prepared) == expected_len, "buffer is not the raw cue slice"
    assert app._next_plan is not None and app._next_plan.skip
    assert any(c[0] == "set_status" and "no stretch" in c[1] for c in app._next_panel.calls)
    print("  PASS prepare skip: raw cue audio stored, no rubberband, status set")


if __name__ == "__main__":
    test_skip_mix_arms_no_restore_ramp()
    test_stretch_mix_arms_ramp_from_matched_bpm()
    test_skip_mix_immediate_when_unaligned()
    test_prepare_skip_stores_raw_audio()
    print("\nAll skip-mix tests passed.")
