"""Headless tests for the pre-listen / PFL cue (L, [ , ]). Covers the device-
INDEPENDENT logic only — the CuePlayer callback/seek/stop-silent math, the linear
resample helper, and the app-level toggle / epoch-guard / lifecycle / seek-key
wiring. The actual second-stream audio output, the 44100->device-rate fallback,
and mid-set device-loss are device-dependent and verified manually with real
headphones (see CLAUDE.md testing table).

Driven with CuePlayer.__new__ (no stream opened) and AutoMixApp.__new__ with a
fake CuePlayer + fake panels — no audio device, no Textual mount."""

import sys
import time
import threading
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))
from automix.app import AutoMixApp, CUE_SEEK_SECONDS
from automix.cue_player import CuePlayer, _resample
from automix.audio_engine import SAMPLE_RATE, MAX_GAIN

NEXT_LEN = SAMPLE_RATE * 4


# ===========================================================================
# CuePlayer headless (no stream): build via __new__ and drive _callback directly
# ===========================================================================

def build_cue(play_rate=SAMPLE_RATE):
    cue = CuePlayer.__new__(CuePlayer)
    cue._lock = threading.Lock()
    cue._audio = None
    cue._pos = 0
    cue._playing = False
    cue._dead = False
    cue._channels = 2
    cue._play_rate = play_rate
    cue._volume = 1.0
    return cue


def test_cue_position_advances():
    cue = build_cue()
    cue.play(np.ones((1000, 2), dtype=np.float32))
    assert cue.is_playing and cue._pos == 0
    out = np.zeros((400, 2), dtype=np.float32)
    cue._callback(out, 400, None, None)
    assert cue._pos == 400 and np.all(out == 1.0)
    print("  PASS CuePlayer: position advances, callback fills from buffer")


def test_cue_stop_silent_at_end_no_loop():
    cue = build_cue()
    cue.play(np.ones((100, 2), dtype=np.float32))
    out = np.zeros((40, 2), dtype=np.float32)
    cue._callback(out, 40, None, None)   # pos 40
    cue._callback(out, 40, None, None)   # pos 80
    tail = np.zeros((40, 2), dtype=np.float32)
    cue._callback(tail, 40, None, None)  # only 20 real samples remain
    assert np.all(tail[:20] == 1.0), "last real samples not emitted"
    assert np.all(tail[20:] == 0.0), "did not pad with silence past the end"
    assert not cue.is_playing, "should stop (not loop) at end of buffer"
    # A further callback stays silent (no wrap-around / loop).
    again = np.full((40, 2), 9.0, dtype=np.float32)
    cue._callback(again, 40, None, None)
    assert np.all(again == 0.0), "looped instead of staying silent"
    print("  PASS CuePlayer: stop-silent at end, no loop")


def test_cue_seek_clamps():
    cue = build_cue()
    cue.play(np.ones((SAMPLE_RATE, 2), dtype=np.float32))   # 1.0s
    out = np.zeros((SAMPLE_RATE // 2, 2), dtype=np.float32)
    cue._callback(out, SAMPLE_RATE // 2, None, None)        # pos ~0.5s
    cue.seek(-2.0)
    assert cue._pos == 0, f"seek back should clamp to 0, got {cue._pos}"
    cue.seek(10.0)
    assert cue._pos == SAMPLE_RATE, f"seek fwd should clamp to end, got {cue._pos}"
    cue.seek(-0.1)
    assert cue._pos == SAMPLE_RATE - int(0.1 * SAMPLE_RATE), "interior seek math wrong"
    # Seek is a no-op when nothing is cueing.
    cue.stop()
    cue.seek(5.0)
    assert cue._pos == 0
    print("  PASS CuePlayer: seek clamps to [0, end] and no-ops when stopped")


def test_cue_dead_is_silent():
    cue = build_cue()
    cue.play(np.ones((1000, 2), dtype=np.float32))
    cue._dead = True
    out = np.full((40, 2), 9.0, dtype=np.float32)
    cue._callback(out, 40, None, None)
    assert np.all(out == 0.0), "dead player must emit silence"
    print("  PASS CuePlayer: dead device emits silence")


def test_cue_volume_scales_output():
    cue = build_cue()
    cue.play(np.ones((1000, 2), dtype=np.float32))
    cue._volume = 0.25
    out = np.zeros((40, 2), dtype=np.float32)
    cue._callback(out, 40, None, None)
    assert np.allclose(out, 0.25), f"cue gain 0.25 should scale 1.0 -> 0.25, got {out[0,0]}"
    cue.set_volume(9.0); assert cue.volume == 1.0
    cue.set_volume(-3.0); assert cue.volume == 0.0
    print("  PASS CuePlayer: callback scales by gain, set_volume clamps 0..1")


def test_resample_helper():
    assert _resample(np.ones((100, 2), dtype=np.float32), 44100, 44100).shape == (100, 2)
    up = _resample(np.ones((100, 2), dtype=np.float32), 44100, 48000)
    assert up.shape[0] == round(100 * 48000 / 44100) == 109
    # Endpoints preserved; constant signal stays constant.
    assert abs(up[0, 0] - 1.0) < 1e-6 and abs(up[-1, 0] - 1.0) < 1e-6
    assert _resample(np.zeros((0, 2), dtype=np.float32), 44100, 48000).shape == (0, 2)
    print("  PASS resample: length scales by rate ratio, endpoints preserved")


# ===========================================================================
# App-level: toggle / epoch / lifecycle / seek keys with a fake CuePlayer
# ===========================================================================

class FakeCue:
    def __init__(self, playing=False, dead=False, pos=0.0, dur=0.0):
        self._playing, self._dead, self._pos, self._dur = playing, dead, pos, dur
        self.play_bufs, self.seeks, self.stop_calls = [], [], 0
        self._volume = 1.0
    @property
    def is_playing(self): return self._playing
    @property
    def is_dead(self): return self._dead
    @property
    def position_seconds(self): return self._pos
    @property
    def duration_seconds(self): return self._dur
    @property
    def volume(self): return self._volume
    def set_volume(self, v): self._volume = max(0.0, min(1.0, v))
    def play(self, buf): self.play_bufs.append(buf); self._playing = True
    def stop(self): self.stop_calls += 1; self._playing = False
    def seek(self, d): self.seeks.append(d)
    def close(self): pass


class FakeEngine:
    def __init__(self):
        self._audio = np.full((NEXT_LEN, 2), 0.5, dtype=np.float32)
        self._volume = 1.0
    @property
    def volume(self): return self._volume
    def set_volume(self, v): self._volume = max(0.0, min(MAX_GAIN, v))  # master can boost
    def load_audio(self, path): return self._audio


class FakeNextPanel:
    def __init__(self, raw_cue=0.0):
        self.raw_cue = raw_cue
        self.cue_states = []
    def set_cue_state(self, playing, pos, dur): self.cue_states.append((playing, pos, dur))
    def clear(self): pass


class FakeKey:
    def __init__(self, character, key=None):
        self.character = character
        self.key = key if key is not None else character
        self.defaulted = self.stopped = False
    def prevent_default(self): self.defaulted = True
    def stop(self): self.stopped = True


def build_cue_app(has_cue=True, has_next=True, raw_cue=0.0,
                  playing=False, dead=False, loading=False):
    app = AutoMixApp.__new__(AutoMixApp)
    app.engine = FakeEngine()
    app._cue = FakeCue(playing=playing, dead=dead) if has_cue else None
    app._cue_epoch = 0
    app._cue_loading = loading
    app._cue_dead_reported = False
    app._next_path = "/next.mp3" if has_next else None
    app._input_mode = ""
    app._auto_armed = False
    app._emergency_fired = False
    app._statuses = []
    app._status = lambda m: app._statuses.append(m)
    app.call_from_thread = lambda fn, *a: fn(*a)
    panel = FakeNextPanel(raw_cue=raw_cue)
    app.query_one = lambda sel, _c=None: panel
    app._panel = panel
    return app


def _toggle_and_wait(app):
    AutoMixApp.action_cue_toggle(app)
    for _ in range(200):
        if app._cue and app._cue.play_bufs:
            break
        time.sleep(0.01)


def test_toggle_no_device():
    app = build_cue_app(has_cue=False)
    AutoMixApp.action_cue_toggle(app)
    assert "No cue device" in app._statuses[-1]
    print("  PASS toggle: no cue device reported, no crash")


def test_toggle_dead_device():
    app = build_cue_app(dead=True)
    AutoMixApp.action_cue_toggle(app)
    assert "lost" in app._statuses[-1] and app._cue.stop_calls == 0
    print("  PASS toggle: dead device reported")


def test_toggle_no_next():
    app = build_cue_app(has_next=False)
    AutoMixApp.action_cue_toggle(app)
    assert "No next track" in app._statuses[-1] and not app._cue.play_bufs
    print("  PASS toggle: no next track rejected")


def test_toggle_start_plays_from_raw_cue():
    app = build_cue_app(raw_cue=1.0)
    _toggle_and_wait(app)
    assert len(app._cue.play_bufs) == 1, "cue.play not called"
    buf = app._cue.play_bufs[0]
    cue_sample = int(1.0 * SAMPLE_RATE)
    assert len(buf) == NEXT_LEN - cue_sample, "buffer not sliced at the RAW cue"
    assert app._cue_loading is False, "loading flag should clear on playback"
    assert app._cue_epoch == 1, "epoch should advance once for the start"
    print("  PASS toggle: decodes + plays from raw cue, loading cleared")


def test_toggle_stop_when_playing():
    app = build_cue_app(playing=True)
    AutoMixApp.action_cue_toggle(app)
    assert app._cue.stop_calls == 1 and not app._cue.is_playing
    assert app._cue_epoch == 1, "stop must bump epoch to orphan any decode"
    assert "stopped" in app._statuses[-1]
    print("  PASS toggle: playing -> stop, epoch bumped")


def test_double_tap_during_load_stops():
    # Second L while the decode is still in flight (loading=True, not yet playing)
    # must read as STOP, not launch a second decode.
    app = build_cue_app(loading=True, playing=False)
    AutoMixApp.action_cue_toggle(app)
    assert app._cue.stop_calls == 1 and app._cue_loading is False
    assert not app._cue.play_bufs, "double-tap should not start a second decode"
    print("  PASS toggle: double-tap during decode stops")


def test_epoch_guard_discards_stale_playback():
    # A decode that completes after the slot changed (epoch advanced) must not play.
    app = build_cue_app()
    buf = np.zeros((10, 2), dtype=np.float32)
    app._cue_epoch = 5                       # slot changed since the worker launched
    AutoMixApp._start_cue_playback(app, buf, "/next.mp3", my_epoch=3)
    assert not app._cue.play_bufs, "stale decode (epoch mismatch) must be discarded"
    print("  PASS epoch: stale decode discarded on hand-off")


def test_stop_cue_mechanism():
    app = build_cue_app(playing=True)
    app._cue_loading = True
    AutoMixApp._stop_cue(app)
    assert app._cue.stop_calls == 1
    assert app._cue_loading is False
    assert app._cue_epoch == 1
    # Safe with no device.
    app2 = build_cue_app(has_cue=False)
    AutoMixApp._stop_cue(app2)   # must not raise
    print("  PASS _stop_cue: stops, clears loading, bumps epoch, no-device safe")


def test_seek_keys_via_character():
    # Italian-layout brackets arrive as event.character "[" / "]" (AltGr-composed);
    # matching must key off the character, not event.key.
    app = build_cue_app(playing=True)
    ev = FakeKey("[", key="something-else")
    AutoMixApp.on_key(app, ev)
    assert app._cue.seeks == [-CUE_SEEK_SECONDS] and ev.defaulted and ev.stopped
    ev2 = FakeKey("]", key="whatever")
    AutoMixApp.on_key(app, ev2)
    assert app._cue.seeks[-1] == CUE_SEEK_SECONDS
    print("  PASS seek keys: [ / ] matched on character, seek -5 / +5")


def test_seek_keys_ignored_when_not_playing():
    app = build_cue_app(playing=False)
    ev = FakeKey("[")
    AutoMixApp.on_key(app, ev)
    assert app._cue.seeks == [] and not ev.defaulted, "must fall through when not cueing"
    print("  PASS seek keys: ignored when cue not playing")


def test_master_volume_keys():
    app = build_cue_app()
    AutoMixApp.on_key(app, FakeKey(","))   # , = down
    assert abs(app.engine.volume - 0.95) < 1e-9, app.engine.volume
    assert "Master volume 95%" in app._statuses[-1]
    # . = up, can boost above 100%
    AutoMixApp.on_key(app, FakeKey("."))
    AutoMixApp.on_key(app, FakeKey("."))
    assert abs(app.engine.volume - 1.05) < 1e-9, f"should boost past 1.0, got {app.engine.volume}"
    assert "(boost)" in app._statuses[-1], "status should flag boost above 100%"
    print("  PASS master volume: , / . adjust engine gain, boosts past 100%")


def test_cue_volume_keys():
    app = build_cue_app()
    ev = FakeKey("9")
    AutoMixApp.on_key(app, ev)
    assert abs(app._cue.volume - 0.95) < 1e-9 and ev.defaulted
    assert "Cue volume 95%" in app._statuses[-1]
    AutoMixApp.on_key(app, FakeKey("0"))
    assert abs(app._cue.volume - 1.0) < 1e-9
    print("  PASS cue volume: 9 / 0 adjust cue gain")


def test_cue_volume_keys_no_device():
    app = build_cue_app(has_cue=False)
    ev = FakeKey("9")
    AutoMixApp.on_key(app, ev)
    assert not ev.defaulted, "9/0 must fall through when no cue device"
    print("  PASS cue volume: 9 / 0 ignored with no cue device")


if __name__ == "__main__":
    test_cue_position_advances()
    test_cue_stop_silent_at_end_no_loop()
    test_cue_seek_clamps()
    test_cue_dead_is_silent()
    test_cue_volume_scales_output()
    test_resample_helper()
    test_toggle_no_device()
    test_toggle_dead_device()
    test_toggle_no_next()
    test_toggle_start_plays_from_raw_cue()
    test_toggle_stop_when_playing()
    test_double_tap_during_load_stops()
    test_epoch_guard_discards_stale_playback()
    test_stop_cue_mechanism()
    test_seek_keys_via_character()
    test_seek_keys_ignored_when_not_playing()
    test_master_volume_keys()
    test_cue_volume_keys()
    test_cue_volume_keys_no_device()
    print("\nAll cue tests passed.")
