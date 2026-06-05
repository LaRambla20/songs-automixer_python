"""Headless tests for the auto/emergency mix: the _tick detector that crossfades
the playing track into the next one as it enters its final EMERGENCY_SECONDS.

Covers the four cases (raw next / preparing / prepared / no-queue), every guard,
the per-track one-shot, the fade clamps, the case-3 bar-align 3 s-fade fallback,
and the _next_song_in_folder picker (mid-folder + end-of-folder). Driven with a
stubbed engine + fake panels, no audio device or Textual mount — the async decode
of the as-is path is bypassed by calling its UI-thread finisher directly."""

import os
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))
import automix.app as app_mod
from automix.app import AutoMixApp, EMERGENCY_SECONDS, EMERGENCY_MIN_FADE
from automix.audio_engine import State, SAMPLE_RATE
from automix.transition import plan_transition


class _SyncThread:
    """Drop-in for threading.Thread that runs the target inline on .start(), so the
    as-is decode worker is deterministic in a probe (no join/sleep races)."""
    def __init__(self, target=None, daemon=None): self._t = target
    def start(self): self._t()


class FakeEngine:
    def __init__(self):
        self.state = State.PLAYING
        self.paused = False
        self.position = 0
        self.duration = 0
        self.start_mix_calls = []
        self._audio = np.zeros((SAMPLE_RATE * 4, 2), dtype=np.float32)

    def start_mix(self, buf, fade, scheduled_start_sample=None):
        self.start_mix_calls.append((buf, fade, scheduled_start_sample))

    def load_audio(self, path):
        return self._audio


class FakeNextPanel:
    def __init__(self, raw_cue=0.0, fade=15.0, restore=20.0, cue_snapped=True):
        self.raw_cue = raw_cue
        self.cue = raw_cue
        self.fade = fade
        self.restore = restore
        self.cue_snapped = cue_snapped
        self.calls = []
    def clear(self): self.calls.append(("clear",))
    def set_status(self, s): self.calls.append(("set_status", s))
    def set_display_bpm(self, b): self.calls.append(("set_display_bpm", b))


class FakeNowPanel:
    def __init__(self): self.calls = []
    def set_track(self, *a, **k): self.calls.append(("set_track", a, k))
    def clear_mix_from(self): self.calls.append(("clear_mix_from",))
    def set_auto(self, on): self.calls.append(("set_auto", on))
    def clear(self): self.calls.append(("clear",))


def build_app(next_panel=None):
    app = AutoMixApp.__new__(AutoMixApp)
    app.engine = FakeEngine()
    app.library = {}
    app._now_path = "/music/a.mp3"
    app._now_bpm = 128.0
    app._now_key = "A min"
    app._now_downbeats = []
    app._next_path = None
    app._next_bpm = 0.0
    app._next_key = ""
    app._next_downbeats = []
    app._next_prepared = None
    app._next_plan = None
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
    app._songs_in_view = []
    app._fx_enabled = False
    app._auto_armed = True            # armed by default for these tests
    app._emergency_fired = False

    app._now_panel = FakeNowPanel()
    app._next_panel = next_panel or FakeNextPanel()
    app._statuses = []
    app._status = lambda m: app._statuses.append(m)
    app.call_from_thread = lambda fn, *a: fn(*a)
    def fake_query_one(selector, _cls=None):
        return app._now_panel if "now-playing" in selector else app._next_panel
    app.query_one = fake_query_one
    return app


def _arm_window(app, remaining_s=5.0):
    """Put the engine inside the final window so the detector would fire."""
    app.engine.state = State.PLAYING
    app.engine.duration = SAMPLE_RATE * 200
    app.engine.position = app.engine.duration - int(remaining_s * SAMPLE_RATE)


# ---------------------------------------------------------------------------
# Case classification
# ---------------------------------------------------------------------------

def test_case_classification():
    # Case 4: nothing queued.
    app = build_app()
    _arm_window(app)
    app._next_path = None
    d = app._emergency_decision()
    assert d and d["case"] == 4, f"expected case 4, got {d}"

    # Case 3: a prepared buffer present.
    app = build_app()
    _arm_window(app)
    app._next_path = "/music/b.mp3"
    app._next_prepared = np.zeros((10, 2), dtype=np.float32)
    d = app._emergency_decision()
    assert d and d["case"] == 3, f"expected case 3, got {d}"

    # Case 2: queued + preparing (no buffer yet).
    app = build_app(FakeNextPanel(raw_cue=3.0))
    _arm_window(app)
    app._next_path = "/music/b.mp3"
    app._preparing = True
    d = app._emergency_decision()
    assert d and d["case"] == 2 and d["cue"] == 3.0, f"expected case 2 @ raw_cue, got {d}"

    # Case 1: queued raw (not preparing, not prepared).
    app = build_app(FakeNextPanel(raw_cue=2.5))
    _arm_window(app)
    app._next_path = "/music/b.mp3"
    d = app._emergency_decision()
    assert d and d["case"] == 1 and d["cue"] == 2.5, f"expected case 1 @ raw_cue, got {d}"
    print("  PASS classification: case 4 / 3 / 2 / 1 selected from NEXT-slot state")


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def test_guards_block_firing():
    def fresh():
        app = build_app()
        _arm_window(app)
        app._next_path = "/music/b.mp3"
        return app

    # Sanity: the fresh fixture WOULD fire.
    assert fresh()._emergency_decision() is not None

    a = fresh(); a._auto_armed = False
    assert a._emergency_decision() is None, "fired while disarmed"

    a = fresh(); a._emergency_fired = True
    assert a._emergency_decision() is None, "fired with one-shot already spent"

    a = fresh(); a.engine.state = State.IDLE
    assert a._emergency_decision() is None, "fired while IDLE"

    a = fresh(); a.engine.state = State.MIXING
    assert a._emergency_decision() is None, "fired while MIXING"

    a = fresh(); a.engine.paused = True
    assert a._emergency_decision() is None, "fired while paused"

    a = fresh(); a._mix_scheduled = True
    assert a._emergency_decision() is None, "fired while a mix is scheduled"

    a = fresh(); a._t_restore_start = 1.0
    assert a._emergency_decision() is None, "fired while restoring"

    # Outside the window (remaining just over the budget) → quiet.
    a = fresh(); a.engine.position = a.engine.duration - int((EMERGENCY_SECONDS + 1) * SAMPLE_RATE)
    assert a._emergency_decision() is None, "fired outside the final window"
    print("  PASS guards: disarmed/spent/IDLE/MIXING/paused/scheduled/restoring/early all quiet")


# ---------------------------------------------------------------------------
# One-shot lifecycle
# ---------------------------------------------------------------------------

def test_one_shot_fires_once_and_rearms():
    app = build_app()
    _arm_window(app)
    app._next_path = None  # case 4, but stub the commit so no thread spawns
    # Make case 4 land on a real source so it doesn't early-return on exhaustion.
    app._next_song_in_folder = lambda p: "/music/c.mp3"
    fired = []
    app._emergency_commit_asis = lambda src, cue, case: fired.append((src, case))

    app._maybe_emergency_mix()
    assert app._emergency_fired, "one-shot not claimed"
    assert fired == [("/music/c.mp3", 4)], f"case 4 did not commit: {fired}"

    # Second call in the same window must NOT re-fire.
    app._maybe_emergency_mix()
    assert fired == [("/music/c.mp3", 4)], "re-fired while one-shot spent"

    # A now-playing swap re-arms it.
    app._apply_now_swap(("/music/c.mp3", 120.0, "C", []))
    assert not app._emergency_fired, "one-shot not re-armed on swap"
    print("  PASS one-shot: fires once, blocks re-fire, re-arms on now-playing swap")


def test_case2_drops_prep():
    app = build_app(FakeNextPanel(raw_cue=1.0))
    _arm_window(app)
    app._next_path = "/music/b.mp3"
    app._preparing = True
    app._prep_animating = True
    app._next_prepared = None
    app._prep_epoch = 7
    committed = []
    app._emergency_commit_asis = lambda src, cue, case: committed.append((src, cue, case))

    app._maybe_emergency_mix()
    assert app._prep_epoch == 8, "prep epoch not bumped (worker not orphaned)"
    assert not app._preparing and not app._prep_animating, "prep flags not cleared"
    assert committed == [("/music/b.mp3", 1.0, 2)], f"case 2 commit wrong: {committed}"
    print("  PASS case 2: in-flight prep dropped (epoch bump), mixes as-is from raw cue")


def test_case4_exhausted_reports_once():
    app = build_app()
    _arm_window(app)
    app._next_path = None
    app._next_song_in_folder = lambda p: None  # last song in folder
    committed = []
    app._emergency_commit_asis = lambda *a: committed.append(a)

    app._maybe_emergency_mix()
    assert app._emergency_fired, "one-shot must be claimed so the report prints once"
    assert not committed, "exhausted case must not commit a mix"
    assert any("last song in folder" in s for s in app._statuses), app._statuses
    print("  PASS case 4 exhausted: reports once, no mix, one-shot claimed (no tick spam)")


# ---------------------------------------------------------------------------
# Commit mechanics — prepared (case 3)
# ---------------------------------------------------------------------------

def test_prepared_fade_clamp_immediate():
    # No downbeats → no bar-align → immediate; fade clamps to the runway.
    app = build_app(FakeNextPanel(fade=15.0, cue_snapped=False))
    app.engine.state = State.PLAYING
    app.engine.duration = SAMPLE_RATE * 200
    app.engine.position = app.engine.duration - int(5.0 * SAMPLE_RATE)  # 5 s left
    app._next_path = "/music/b.mp3"
    app._next_bpm = 130.0
    app._next_plan = plan_transition(128.0, 130.0, 30.0)   # stretch
    matched_bpm = app._next_plan.matched_bpm   # _commit_mix nulls _next_plan
    app._next_prepared = np.zeros((10, 2), dtype=np.float32)

    app._emergency_commit_prepared()
    buf, fade, scheduled = app.engine.start_mix_calls[-1]
    assert scheduled is None, "should fire immediately with no downbeats"
    assert abs(fade - 5.0) < 0.05, f"fade should clamp to 5 s runway, got {fade}"
    # Stretch plan arms the restore ramp from matched_bpm → next_bpm.
    assert abs(app._restore_from_bpm - matched_bpm) < 1e-6
    assert abs(app._restore_to_bpm - 130.0) < 1e-6
    print("  PASS case 3 immediate: fade clamped to runway, stretch ramp armed")


def test_prepared_bar_align_and_fallback():
    # Downbeat well within the runway (leaves >= MIN_FADE) → scheduled.
    app = build_app(FakeNextPanel(fade=15.0, cue_snapped=True))
    app.engine.state = State.PLAYING
    app.engine.duration = SAMPLE_RATE * 200
    app.engine.position = app.engine.duration - int(8.0 * SAMPLE_RATE)  # 8 s left
    db = app.engine.position + int(1.0 * SAMPLE_RATE)                   # 1 s ahead
    app._now_downbeats = [db]
    app._next_downbeats = [0]
    app._next_path = "/music/b.mp3"
    app._next_bpm = 128.0
    app._next_plan = plan_transition(128.0, 128.0, 30.0)   # skip (no ramp)
    app._next_prepared = np.zeros((10, 2), dtype=np.float32)

    app._emergency_commit_prepared()
    buf, fade, scheduled = app.engine.start_mix_calls[-1]
    assert scheduled == db, f"should bar-align to the downbeat, got {scheduled}"
    # Runway after the downbeat is 7 s; fade = min(panel.fade 15, 7) = 7.
    assert abs(fade - 7.0) < 0.05, f"fade should be runway-after-downbeat (7s), got {fade}"
    assert app._restore_from_bpm == 0.0 and app._restore_to_bpm == 0.0, "skip armed a ramp"

    # Now a downbeat too close to the end (< MIN_FADE left) → fall back to immediate.
    app = build_app(FakeNextPanel(fade=15.0, cue_snapped=True))
    app.engine.state = State.PLAYING
    app.engine.duration = SAMPLE_RATE * 200
    app.engine.position = app.engine.duration - int(8.0 * SAMPLE_RATE)
    near_end = app.engine.duration - int((EMERGENCY_MIN_FADE - 1.0) * SAMPLE_RATE)
    app._now_downbeats = [near_end]
    app._next_downbeats = [0]
    app._next_path = "/music/b.mp3"
    app._next_bpm = 128.0
    app._next_plan = plan_transition(128.0, 128.0, 30.0)
    app._next_prepared = np.zeros((10, 2), dtype=np.float32)

    app._emergency_commit_prepared()
    buf, fade, scheduled = app.engine.start_mix_calls[-1]
    assert scheduled is None, "downbeat too close to end should fall back to immediate"
    print("  PASS case 3 bar-align: schedules valid downbeat, falls back under 3 s fade")


# ---------------------------------------------------------------------------
# Commit mechanics — as-is (cases 1/2/4)
# ---------------------------------------------------------------------------

def test_buffer_downbeats_helper():
    app = build_app()
    app._next_downbeats = [SAMPLE_RATE * 1, SAMPLE_RATE * 2, SAMPLE_RATE * 3]
    skip = plan_transition(128.0, 128.0, 30.0)
    stretch = plan_transition(128.0, 130.0, 30.0)
    assert skip.skip and not stretch.skip

    # SKIP: clean constant shift, pre-cue downbeats dropped.
    cue_sample = SAMPLE_RATE * 2
    assert app._buffer_downbeats(skip, cue_sample) == [0, SAMPLE_RATE * 1]
    # STRETCH: warped timing → no reliable buffer-relative downbeats.
    assert app._buffer_downbeats(stretch, cue_sample) == []
    # No plan (defensive) treated as raw: full rebase from cue 0.
    assert app._buffer_downbeats(None, 0) == app._next_downbeats
    print("  PASS _buffer_downbeats: skip rebases off cue, stretch drops, None=raw")


def test_prepared_swap_rebases_downbeats():
    # SKIP prepared mix, non-zero cue, A has no downbeats → immediate → swap applied.
    app = build_app(FakeNextPanel(raw_cue=2.0, cue_snapped=False))
    app.engine.state = State.PLAYING
    app.engine.duration = SAMPLE_RATE * 200
    app.engine.position = app.engine.duration - int(5.0 * SAMPLE_RATE)
    app._now_downbeats = []
    app._next_path = "/music/b.mp3"
    app._next_bpm = 128.0
    app._next_downbeats = [SAMPLE_RATE * 1, SAMPLE_RATE * 2, SAMPLE_RATE * 3]
    app._next_plan = plan_transition(128.0, 128.0, 30.0)   # skip
    app._next_prepared = np.zeros((10, 2), dtype=np.float32)
    app._emergency_commit_prepared()
    # cue_sample = 2 s; downbeats rebased → [0, 1 s] (the 1 s pre-cue beat dropped).
    assert app._now_downbeats == [0, SAMPLE_RATE * 1], app._now_downbeats

    # STRETCH prepared mix → downbeats dropped (warped buffer).
    app = build_app(FakeNextPanel(raw_cue=2.0, cue_snapped=False))
    app.engine.state = State.PLAYING
    app.engine.duration = SAMPLE_RATE * 200
    app.engine.position = app.engine.duration - int(5.0 * SAMPLE_RATE)
    app._now_downbeats = []
    app._next_path = "/music/b.mp3"
    app._next_bpm = 130.0
    app._next_downbeats = [SAMPLE_RATE * 1, SAMPLE_RATE * 2]
    app._next_plan = plan_transition(128.0, 130.0, 30.0)   # stretch
    app._next_prepared = np.zeros((10, 2), dtype=np.float32)
    app._emergency_commit_prepared()
    assert app._now_downbeats == [], app._now_downbeats
    print("  PASS case 3 swap: skip rebases incoming downbeats, stretch drops them")


def test_asis_finisher_fade_and_swap():
    app = build_app()
    app.engine.state = State.PLAYING
    app.engine.duration = SAMPLE_RATE * 100
    app.engine.position = app.engine.duration - int(5.0 * SAMPLE_RATE)  # 5 s left
    buf = np.zeros((SAMPLE_RATE, 2), dtype=np.float32)

    app._start_emergency_asis(buf, "/music/c.mp3", 120.0, "C maj", [10, 20], case=1)
    bufc, fade, scheduled = app.engine.start_mix_calls[-1]
    assert scheduled is None, "as-is mixes fire immediately"
    assert abs(fade - 5.0) < 0.05, f"fade should be min(10, 5)=5, got {fade}"
    assert app._restore_from_bpm == 0.0 and app._restore_to_bpm == 0.0, "as-is armed a ramp"
    assert app._now_path == "/music/c.mp3", "now-playing swap not applied"
    assert app._next_path is None, "NEXT slot not cleared"
    assert any("(as-is)" in s for s in app._statuses), app._statuses

    # Full-window case: remaining 10 s → fade clamps to the 10 s budget, not more.
    app = build_app()
    app.engine.state = State.PLAYING
    app.engine.duration = SAMPLE_RATE * 100
    app.engine.position = app.engine.duration - int(EMERGENCY_SECONDS * SAMPLE_RATE)
    app._start_emergency_asis(buf, "/music/c.mp3", 0.0, "", [], case=4)
    _, fade, _ = app.engine.start_mix_calls[-1]
    assert abs(fade - EMERGENCY_SECONDS) < 0.05, f"fade should be the 10 s budget, got {fade}"
    print("  PASS as-is: immediate, fade=min(10,remaining), no ramp, swap+clear, status")


def test_asis_rebases_downbeats_off_cue():
    # A non-zero raw cue means the buffer starts at cue_sample; the new now-playing
    # downbeats must be rebased onto the buffer (mirrors the backspin offset), else a
    # later bar-aligned mix off this track lands off the bar.
    app = build_app()
    app.engine.state = State.PLAYING
    app.engine.duration = SAMPLE_RATE * 100
    app.engine.position = app.engine.duration - int(5.0 * SAMPLE_RATE)
    cue_sec = 2.0
    cue_sample = int(cue_sec * SAMPLE_RATE)
    app.library["/music/c.mp3"] = {
        "bpm": 120.0, "key": "C", "beats": [],
        "downbeats": [SAMPLE_RATE, cue_sample, SAMPLE_RATE * 3],  # 1 s (pre-cue) / cue / 3 s
    }

    orig_thread = app_mod.threading.Thread
    app_mod.threading.Thread = _SyncThread
    try:
        app._emergency_commit_asis("/music/c.mp3", cue_sec, case=1)
    finally:
        app_mod.threading.Thread = orig_thread

    # Pre-cue downbeat dropped; the rest rebased onto the buffer (minus cue_sample).
    assert app._now_downbeats == [0, SAMPLE_RATE * 3 - cue_sample], app._now_downbeats
    print("  PASS as-is rebasing: downbeats offset by the cue, pre-cue beat dropped")


def test_asis_finisher_bails_if_not_playing():
    app = build_app()
    app.engine.state = State.IDLE   # track ended while decoding
    app.engine.duration = SAMPLE_RATE * 100
    app.engine.position = SAMPLE_RATE * 99
    buf = np.zeros((SAMPLE_RATE, 2), dtype=np.float32)
    app._start_emergency_asis(buf, "/music/c.mp3", 0.0, "", [], case=1)
    assert not app.engine.start_mix_calls, "committed a mix after the track ended"
    print("  PASS as-is bail: no commit when engine left PLAYING during the decode")


# ---------------------------------------------------------------------------
# Folder picker
# ---------------------------------------------------------------------------

def test_next_song_in_folder():
    app = build_app()
    with tempfile.TemporaryDirectory() as d:
        names = ["01.mp3", "02.mp3", "03.wav", "cover.jpg", "notes.txt"]
        for n in names:
            open(os.path.join(d, n), "w").close()
        first = os.path.join(d, "01.mp3")
        second = os.path.join(d, "02.mp3")
        third = os.path.join(d, "03.wav")

        assert app._next_song_in_folder(first) == second, "should pick the next sorted song"
        assert app._next_song_in_folder(second) == third, "non-audio files must be skipped"
        assert app._next_song_in_folder(third) is None, "last song → None (stop + report)"
    print("  PASS folder pick: next sorted audio, skips non-audio, None at folder end")


if __name__ == "__main__":
    test_case_classification()
    test_guards_block_firing()
    test_one_shot_fires_once_and_rearms()
    test_case2_drops_prep()
    test_case4_exhausted_reports_once()
    test_prepared_fade_clamp_immediate()
    test_prepared_bar_align_and_fallback()
    test_buffer_downbeats_helper()
    test_prepared_swap_rebases_downbeats()
    test_asis_finisher_fade_and_swap()
    test_asis_rebases_downbeats_off_cue()
    test_asis_finisher_bails_if_not_playing()
    test_next_song_in_folder()
    print("\nAll emergency-mix tests passed.")
