"""Headless tests for the backspin transition (B). The action requires a RAW next
track (queued, not preparing, not prepared) and a playing engine that is not mid-
transition; on success it plays a single [SFX + cue audio] buffer and promotes the
next track to now-playing with NO restoration ramp. Driven with a stubbed engine +
fake panels, no audio device or Textual mount."""

import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))
from automix.app import AutoMixApp
from automix.audio_engine import State


NEXT_LEN = 44100 * 4
BACKSPIN_LEN = 22050


class FakeEngine:
    def __init__(self, state=State.PLAYING):
        self.state = state
        self.paused = False
        self.position = 0
        self.duration = 0
        self.play_calls = []
        # next-track audio: distinct value (0.5) so we can tell it apart from the SFX.
        self._audio = np.full((NEXT_LEN, 2), 0.5, dtype=np.float32)
    def play(self, audio):
        self.play_calls.append(audio)
        self.state = State.PLAYING
    def load_audio(self, path):
        return self._audio


class FakeNextPanel:
    def __init__(self, cue=0.0, raw_cue=0.0):
        self.cue = cue            # bar-snapped (mix) cue — backspin must NOT use this
        self.raw_cue = raw_cue    # unsnapped cue — backspin uses this
        self.calls = []
    def clear(self): self.calls.append(("clear",))
    def set_status(self, s): self.calls.append(("set_status", s))
    def set_display_bpm(self, b): self.calls.append(("set_display_bpm", b))


class FakeNowPanel:
    def __init__(self): self.calls = []
    def set_track(self, *a, **k): self.calls.append(("set_track", a, k))
    def clear_mix_from(self): self.calls.append(("clear_mix_from",))
    def clear(self): self.calls.append(("clear",))


def build_app(
    *,
    engine_state=State.PLAYING,
    has_next=True,
    preparing=False,
    prepared=False,
    t_restore_start=0.0,
    backspin_loaded=True,
    cue=0.0,
    raw_cue=None,
    next_downbeats=None,
):
    app = AutoMixApp.__new__(AutoMixApp)
    app.engine = FakeEngine(state=engine_state)
    app.library = {}
    app._now_path = "/old.mp3"
    app._now_bpm = 128.0
    app._now_key = "A min"
    app._now_downbeats = [44100, 88200]
    app._next_path = "/new.mp3" if has_next else None
    app._next_bpm = 124.0
    app._next_key = "C maj"
    app._next_downbeats = (
        next_downbeats if next_downbeats is not None else [0, 88200, 176400]
    )
    app._next_prepared = np.zeros((100, 2), dtype=np.float32) if prepared else None
    app._next_plan = None
    app._preparing = preparing
    app._prep_animating = False
    app._prep_progress = 0.0
    app._prep_epoch = 0
    app._mix_scheduled = False
    app._pending_now_swap = None
    app._restore_from_bpm = 0.0
    app._restore_to_bpm = 0.0
    app._restore_seconds = 30.0
    app._t_restore_start = t_restore_start
    app._songs_in_view = []
    app._backspin_audio = (
        np.ones((BACKSPIN_LEN, 2), dtype=np.float32) if backspin_loaded else None
    )

    next_panel = FakeNextPanel(cue=cue, raw_cue=cue if raw_cue is None else raw_cue)
    now_panel = FakeNowPanel()
    app._statuses = []
    app._status = lambda m: app._statuses.append(m)
    app.call_from_thread = lambda fn, *a: fn(*a)
    def fake_query_one(selector, _cls=None):
        if "now-playing" in selector:
            return now_panel
        return next_panel   # next-track + song-list (latter unused: empty view)
    app.query_one = fake_query_one
    app._next_panel = next_panel
    app._now_panel = now_panel
    return app


def _run_and_wait(app):
    """action_backspin spawns a worker thread; poll briefly for engine.play."""
    AutoMixApp.action_backspin(app)
    for _ in range(200):
        if app.engine.play_calls:
            break
        time.sleep(0.01)


# --- guard tests: each must reject with NO engine.play call -------------------

def test_guard_idle():
    app = build_app(engine_state=State.IDLE)
    AutoMixApp.action_backspin(app)
    assert not app.engine.play_calls
    assert "No track playing" in app._statuses[-1]
    print("  PASS guard: IDLE rejected")


def test_guard_no_next():
    app = build_app(has_next=False)
    AutoMixApp.action_backspin(app)
    assert not app.engine.play_calls
    assert "No next track" in app._statuses[-1]
    print("  PASS guard: no next track rejected")


def test_guard_preparing():
    app = build_app(preparing=True)
    AutoMixApp.action_backspin(app)
    assert not app.engine.play_calls
    assert "being prepared" in app._statuses[-1]
    print("  PASS guard: preparing rejected")


def test_guard_prepared():
    app = build_app(prepared=True)
    AutoMixApp.action_backspin(app)
    assert not app.engine.play_calls
    assert "is prepared" in app._statuses[-1]
    print("  PASS guard: already-prepared rejected")


def test_guard_mixing():
    app = build_app(engine_state=State.MIXING)
    AutoMixApp.action_backspin(app)
    assert not app.engine.play_calls
    assert "Mixing" in app._statuses[-1]
    print("  PASS guard: MIXING rejected")


def test_guard_restoring():
    app = build_app(t_restore_start=123.0)
    AutoMixApp.action_backspin(app)
    assert not app.engine.play_calls
    assert "Restoring" in app._statuses[-1]
    print("  PASS guard: restoration ramp rejected")


def test_guard_sample_not_loaded():
    app = build_app(backspin_loaded=False)
    AutoMixApp.action_backspin(app)
    assert not app.engine.play_calls
    assert "loading" in app._statuses[-1]
    print("  PASS guard: unloaded sample rejected")


# --- happy path --------------------------------------------------------------

def test_happy_path_buffer_and_swap():
    # Snapped (mix) cue is 2.0s, but the RAW cue is 0.0 — backspin must use the raw
    # cue and play the next track from sample 0, ignoring the bar snap.
    app = build_app(cue=2.0, raw_cue=0.0)
    _run_and_wait(app)

    assert len(app.engine.play_calls) == 1, "engine.play not called"
    buf = app.engine.play_calls[0]
    cue_sample = 0   # raw_cue = 0.0 -> whole track appended
    expected_len = BACKSPIN_LEN + (NEXT_LEN - cue_sample)
    assert len(buf) == expected_len, f"buffer length {len(buf)} != {expected_len} (backspin used snapped cue?)"
    # First BACKSPIN_LEN samples are the SFX (value 1.0); the rest is the track (0.5).
    assert np.all(buf[:BACKSPIN_LEN] == 1.0), "SFX not prepended"
    assert np.all(buf[BACKSPIN_LEN:] == 0.5), "cue audio not appended"

    # Now-playing promoted to the former next track; NEXT slot cleared.
    assert app._now_path == "/new.mp3"
    assert app._now_bpm == 124.0
    assert app._now_key == "C maj"
    assert app._next_path is None
    assert app._next_prepared is None and app._next_plan is None
    assert ("clear",) in app._next_panel.calls

    # No restoration ramp (plays at natural tempo).
    assert app._restore_from_bpm == 0.0 and app._restore_to_bpm == 0.0
    assert app._t_restore_start == 0.0
    assert app._mix_scheduled is False

    # NowPlaying.set_track called WITHOUT mix_from (abrupt cut, not a crossfade).
    set_track = [c for c in app._now_panel.calls if c[0] == "set_track"]
    assert set_track and set_track[-1][2].get("mix_from") is None
    print("  PASS happy path: SFX+cue buffer played, swap done, no ramp, no mix_from")


def test_downbeats_offset():
    # Raw cue 1.0s (used by backspin); snapped cue deliberately different (2.0s) to
    # confirm the offset math keys off the raw cue. Downbeat at 0 is before the raw
    # cue and dropped.
    raw_cue_sec = 1.0
    cue_sample = int(raw_cue_sec * 44100)
    app = build_app(cue=2.0, raw_cue=raw_cue_sec, next_downbeats=[0, 88200, 176400])
    _run_and_wait(app)

    offset = BACKSPIN_LEN - cue_sample
    expected = [88200 + offset, 176400 + offset]   # 0 dropped (< cue_sample)
    assert app._now_downbeats == expected, (
        f"downbeats {app._now_downbeats} != offset {expected}"
    )
    print("  PASS downbeats: offset by SFX length off the raw cue, pre-cue downbeat dropped")


if __name__ == "__main__":
    test_guard_idle()
    test_guard_no_next()
    test_guard_preparing()
    test_guard_prepared()
    test_guard_mixing()
    test_guard_restoring()
    test_guard_sample_not_loaded()
    test_happy_path_buffer_and_swap()
    test_downbeats_offset()
    print("\nAll backspin tests passed.")
