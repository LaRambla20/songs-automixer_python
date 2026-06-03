"""Headless tests for the four conflict-handling fixes (A/B/C/D), driving _tick
with a stubbed engine and fake panels â€” no audio device or Textual mount needed."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from automix.app import AutoMixApp, NowPlayingPanel, NextTrackPanel
from automix.audio_engine import State


class FakePanel:
    def __init__(self):
        self.calls = []
    def __getattr__(self, name):
        def rec(*a, **k):
            self.calls.append((name, a, k))
        return rec


class FakeEngine:
    def __init__(self):
        self.state = State.IDLE
        self.paused = False
        self.position = 0
        self.duration = 0
        self.mix_position = 0
        self.mix_duration = 0
        self.volume = 1.0
    def stop(self):
        self.state = State.IDLE


def build_app():
    app = AutoMixApp.__new__(AutoMixApp)
    app.engine = FakeEngine()
    app._now_path = None
    app._now_bpm = 0.0
    app._now_key = ""
    app._now_downbeats = []
    app._next_path = None
    app._next_bpm = 0.0
    app._next_key = ""
    app._next_downbeats = []
    app._next_prepared = None
    app._next_plan = None
    app._preparing = False
    app._mix_scheduled = False
    app._pending_now_swap = None
    app._last_tick_t = time.time()
    app._restore_from_bpm = 0.0
    app._restore_to_bpm = 0.0
    app._restore_seconds = 30.0
    app._t_restore_start = 0.0
    app._prev_engine_state = State.IDLE
    app._prep_animating = False
    app._prep_progress = 0.0
    app._prep_from_bpm = 0.0
    app._prep_to_bpm = 0.0
    app._prep_epoch = 0
    app._cue = None
    app._cue_epoch = 0
    app._cue_loading = False
    app._input_mode = ""
    app._input_buf = ""
    app._songs_in_view = []   # _refresh_match_markers iterates this (no-op when empty)

    app._now_panel = FakePanel()
    app._next_panel = FakePanel()
    app._statuses = []
    app._status = lambda m: app._statuses.append(m)
    def fake_query_one(selector, _cls=None):
        return app._now_panel if "now-playing" in selector else app._next_panel
    app.query_one = fake_query_one
    return app


def test_C_deferred_swap_applies_on_fire():
    app = build_app()
    # Simulate state right after pressing M with a deferred (bar-aligned) mix:
    app.engine.state = State.PLAYING
    app._prev_engine_state = State.PLAYING
    app._now_path = "/old.mp3"; app._now_bpm = 120.0
    app._mix_scheduled = True
    app._pending_now_swap = ("/new.mp3", 128.0, "A min", [100, 200])
    app._restore_from_bpm = 120.0; app._restore_to_bpm = 128.0

    # Tick 1: still waiting (PLAYING). Swap must NOT apply yet.
    app._tick()
    assert app._now_path == "/old.mp3", "swap applied too early"
    assert app._pending_now_swap is not None

    # Tick 2: engine fires the crossfade -> MIXING. Swap must apply now.
    app.engine.state = State.MIXING
    app._tick()
    assert app._now_path == "/new.mp3", f"swap not applied on fire: {app._now_path}"
    assert app._now_bpm == 128.0
    assert app._pending_now_swap is None
    assert not app._mix_scheduled
    assert app._statuses[-1] == "Mixing..."
    print("  PASS C: deferred swap held during wait, applied when crossfade fired")


def test_A_track_finished_cleanup():
    app = build_app()
    app.engine.state = State.PLAYING
    app._prev_engine_state = State.PLAYING
    app._now_path = "/playing.mp3"; app._now_bpm = 120.0; app._now_downbeats = [1, 2]
    # Queued next track + a prepared buffer (rendered against the now-finishing track)
    app._next_path = "/queued.mp3"
    app._next_prepared = "stale-buffer"
    app._prep_epoch = 5

    # Engine reaches end of track -> IDLE
    app.engine.state = State.IDLE
    app._tick()

    assert app._now_path is None, "now-playing not cleared on track end"
    assert app._now_bpm == 0.0
    assert app._next_prepared is None, "stale prepared buffer survived track end"
    assert app._prep_epoch == 6, "epoch not bumped (orphaned prep not invalidated)"
    # NEXT track selection kept, but marked unprepared
    assert app._next_path == "/queued.mp3", "next-track selection lost"
    assert ("clear", (), {}) in app._now_panel.calls, "NowPlaying not cleared"
    assert any(c[0] == "set_status" for c in app._next_panel.calls)
    assert "queued" in app._statuses[-1].lower()
    print("  PASS A: track-end clears now-playing + invalidates prep, keeps next queued")


def test_A_no_cleanup_after_explicit_stop():
    """After action_stop, _now_path is already None; the IDLE transition in _tick
    must be a no-op (not double-fire the finished handler)."""
    app = build_app()
    app.engine.state = State.PLAYING
    app._prev_engine_state = State.PLAYING
    app._now_path = None   # action_stop already cleared it
    app.engine.state = State.IDLE
    app._tick()
    # No status about 'Track finished' should appear
    assert not any("finished" in s.lower() for s in app._statuses)
    print("  PASS A: explicit-stop IDLE transition does not re-fire finished handler")


def test_D_ramp_frozen_while_paused():
    app = build_app()
    app.engine.state = State.PLAYING
    app._prev_engine_state = State.PLAYING
    app._now_path = "/p.mp3"; app._now_bpm = 120.0
    app._restore_from_bpm = 120.0; app._restore_to_bpm = 128.0
    app._restore_seconds = 10.0
    # Arm ramp 2 seconds ago
    app._t_restore_start = time.time() - 2.0
    app._last_tick_t = time.time() - 0.1

    # Not paused: a normal tick should NOT shift the start.
    start_before = app._t_restore_start
    app.engine.paused = False
    app._tick()
    assert abs(app._t_restore_start - start_before) < 0.05, "ramp shifted while not paused"

    # Paused: each tick should push the start forward by dt, freezing elapsed.
    app.engine.paused = True
    app._last_tick_t = time.time() - 0.5   # simulate 0.5s since last tick
    elapsed_before = time.time() - app._t_restore_start
    app._tick()
    elapsed_after = time.time() - app._t_restore_start
    assert elapsed_after < elapsed_before, "paused tick did not freeze the ramp"
    print(f"  PASS D: ramp elapsed held across pause ({elapsed_before:.2f}s -> {elapsed_after:.2f}s)")


def test_stop_invalidates_completed_prep():
    """Stop must drop a COMPLETED prep buffer, not just orphan an in-flight one.
    Otherwise Stop -> Enter(new track) -> M would mix a buffer whose start_rate
    was rendered against the stopped track's BPM (wrong tempo)."""
    app = build_app()
    app.engine.state = State.PLAYING
    app._now_path = "/playing.mp3"; app._now_bpm = 120.0
    app._next_path = "/queued.mp3"
    app._next_prepared = "buffer-rendered-against-120bpm"
    epoch_before = app._prep_epoch

    # Bind the real action_stop (build_app stubs query_one, _status).
    AutoMixApp.action_stop(app)

    assert app._next_prepared is None, "Stop kept a stale completed buffer"
    assert app._prep_epoch == epoch_before + 1, "Stop didn't bump epoch"
    assert app._now_path is None
    assert app._next_path == "/queued.mp3", "Stop dropped the queued next track"
    assert any(c[0] == "set_status" for c in app._next_panel.calls), \
        "NEXT panel not reset to not-prepared"
    print("  PASS Stop: completed prep buffer invalidated, next track kept unprepared")


def test_queue_now_playing_song_as_next():
    """Mixing a song with itself: N on the currently-playing track is allowed
    (no _now_path guard), and Prepare would compute start_rate == 1.0."""
    app = build_app()
    app.engine.state = State.PLAYING
    app._now_path = "/song.mp3"; app._now_bpm = 124.0; app._now_key = "C maj"
    app._now_downbeats = [100, 200, 300]
    # Same song selected in the table
    app._selected_song = lambda: "/song.mp3"
    app.library = {"/song.mp3": {"bpm": 124.0, "key": "C maj",
                                 "beats": [50, 100, 150, 200],
                                 "downbeats": [100, 200, 300]}}

    AutoMixApp.action_load_next(app)

    assert app._next_path == "/song.mp3", "N on now-playing song was blocked"
    assert app._next_bpm == 124.0
    # Prepare's start_rate for this pairing:
    start_rate = app._now_bpm / app._next_bpm if app._next_bpm > 0 else 1.0
    assert abs(start_rate - 1.0) < 1e-9, f"start_rate should be 1.0, got {start_rate}"
    # And the restoration ramp would NOT arm (same BPM both ends):
    would_arm = abs(app._now_bpm - app._next_bpm) > 0.01
    assert not would_arm, "self-mix should not trigger a restoration ramp"
    print("  PASS self-mix: now-playing song queues as next, start_rate=1.0, no ramp")


def test_phase_banner_states():
    """_tick selects one of four NowPlaying phase banners from engine state +
    flags. WAITING is the deferred bar-wait window (PLAYING + _mix_scheduled),
    distinct from MIXING / RESTORING / Ready."""
    def last_phase(app):
        phases = [c for c in app._now_panel.calls if c[0] == "set_phase"]
        return phases[-1][1][0] if phases else None

    # WAITING: deferred mix armed, engine still PLAYING (crossfade not yet fired).
    app = build_app()
    app.engine.state = State.PLAYING
    app._prev_engine_state = State.PLAYING
    app._now_path = "/old.mp3"; app._now_bpm = 120.0
    app._mix_scheduled = True
    app._pending_now_swap = ("/new.mp3", 128.0, "A min", [100, 200])
    app._restore_from_bpm = 120.0; app._restore_to_bpm = 128.0
    app._tick()
    assert "WAITING for downbeat" in last_phase(app), last_phase(app)
    assert app._now_path == "/old.mp3", "swap must not apply during the wait"

    # MIXING: crossfade actually running.
    app = build_app()
    app.engine.state = State.MIXING
    app._prev_engine_state = State.MIXING
    app._now_path = "/new.mp3"; app._now_bpm = 128.0
    app._restore_from_bpm = 120.0; app._restore_to_bpm = 128.0
    app._tick()
    assert "MIXING the two tracks" in last_phase(app), last_phase(app)

    # RESTORING: tempo ramp running after the crossfade (PLAYING, ramp armed).
    app = build_app()
    app.engine.state = State.PLAYING
    app._prev_engine_state = State.PLAYING
    app._now_path = "/new.mp3"; app._now_bpm = 128.0
    app._restore_from_bpm = 120.0; app._restore_to_bpm = 128.0
    app._restore_seconds = 30.0
    app._t_restore_start = time.time() - 1.0   # ~3% through, well short of done
    app._tick()
    assert "RESTORING original tempo" in last_phase(app), last_phase(app)

    # Ready: plain playback, nothing pending.
    app = build_app()
    app.engine.state = State.PLAYING
    app._prev_engine_state = State.PLAYING
    app._now_path = "/new.mp3"; app._now_bpm = 128.0
    app._tick()
    assert "Ready to mix another song" in last_phase(app), last_phase(app)
    print("  PASS phase banner: WAITING / MIXING / RESTORING / Ready selected correctly")


def test_track_finish_cancels_input_mode():
    """A C/F/R text entry in progress when the track ends naturally must be
    cancelled, so a later keystroke doesn't re-render a stale input prompt over
    the 'Track finished' status."""
    app = build_app()
    app.engine.state = State.PLAYING
    app._prev_engine_state = State.PLAYING
    app._now_path = "/playing.mp3"; app._now_bpm = 120.0
    app._input_mode = "cue"
    app._input_buf = "12.3"

    app.engine.state = State.IDLE
    app._tick()

    assert app._input_mode == "", "input mode not cancelled on track finish"
    assert app._input_buf == "", "input buffer not cleared on track finish"
    print("  PASS input-cancel: C/F/R entry cancelled when track finishes")


if __name__ == "__main__":
    test_C_deferred_swap_applies_on_fire()
    test_A_track_finished_cleanup()
    test_A_no_cleanup_after_explicit_stop()
    test_D_ramp_frozen_while_paused()
    test_stop_invalidates_completed_prep()
    test_queue_now_playing_song_as_next()
    test_phase_banner_states()
    test_track_finish_cancels_input_mode()
    print("\nAll conflict-handling tests passed.")
