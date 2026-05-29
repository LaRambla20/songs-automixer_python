"""Verify the epoch guard pattern: a prep worker started for one track must NOT
install its buffer or push progress updates after a subsequent N / P / stale
event has invalidated it."""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class FakeApp:
    """Mirrors just the parts of AutoMixApp the epoch check touches."""
    def __init__(self):
        self._prep_epoch = 0
        self._prep_progress = 0.0
        self._next_prepared = None
        self._preparing = False
        self._on_prepared_called = False

    # Mirror of the closure pattern in action_prepare_mix's _work / _on_progress.
    def start_prep(self, fake_render_seconds: float):
        self._preparing = True
        self._prep_epoch += 1
        my_epoch = self._prep_epoch
        progress_writes = {"accepted": 0, "rejected": 0}

        def _on_progress(p):
            if self._prep_epoch != my_epoch:
                progress_writes["rejected"] += 1
                return
            progress_writes["accepted"] += 1
            if p > self._prep_progress:
                self._prep_progress = p

        def _work():
            # Simulate rubberband running for fake_render_seconds, emitting
            # progress every 50ms.
            t0 = time.time()
            while time.time() - t0 < fake_render_seconds:
                pct = (time.time() - t0) / fake_render_seconds
                _on_progress(min(1.0, pct))
                time.sleep(0.05)
            _on_progress(1.0)
            # The "buffer ready" install
            buf = f"buf-for-epoch-{my_epoch}"
            if self._prep_epoch != my_epoch:
                return
            self._next_prepared = buf
            self._preparing = False
            self._on_prepared_called = True

        t = threading.Thread(target=_work, daemon=True)
        t.start()
        return t, progress_writes


def test_n_during_prep_drops_buffer():
    app = FakeApp()
    # Start prep that takes 0.5 seconds.
    thread, writes = app.start_prep(0.5)
    # Wait briefly so the worker is mid-stream.
    time.sleep(0.15)
    # Simulate pressing N: bump epoch, clear flags
    app._prep_epoch += 1
    app._preparing = False
    app._prep_progress = 0.0
    # Wait for the orphaned worker to complete its render
    thread.join(timeout=2.0)

    assert app._next_prepared is None, (
        f"BUG: orphaned worker installed its buffer: {app._next_prepared}"
    )
    assert app._on_prepared_called is False
    assert writes["rejected"] > 0, "no progress writes were rejected"
    print(f"  PASS: orphaned worker dropped buffer; {writes['rejected']} "
          f"progress writes ignored ({writes['accepted']} accepted pre-epoch-bump)")


def test_prep_completes_normally_when_no_bump():
    app = FakeApp()
    thread, writes = app.start_prep(0.2)
    thread.join(timeout=2.0)
    assert app._next_prepared == "buf-for-epoch-1"
    assert app._on_prepared_called is True
    assert writes["rejected"] == 0
    print(f"  PASS: undisturbed prep installs buffer ({writes['accepted']} updates accepted)")


def test_double_n_during_prep():
    """User presses N, then N again rapidly during the first prep â€” only the
    final buffer (which here doesn't exist because nothing was queued again)
    should win. Both orphans drop."""
    app = FakeApp()
    t1, w1 = app.start_prep(0.3)
    time.sleep(0.05)
    app._prep_epoch += 1   # first N
    time.sleep(0.05)
    app._prep_epoch += 1   # second N
    t1.join(timeout=2.0)
    assert app._next_prepared is None
    assert w1["rejected"] > 0
    print(f"  PASS: two superseding events still drop the buffer")


def test_mark_stale_during_prep():
    """User changes Cue mid-prep -> _mark_stale bumps the epoch -> the running
    worker's buffer is discarded when it finishes (would otherwise install a
    buffer rendered with the OLD cue value)."""
    app = FakeApp()
    t1, w1 = app.start_prep(0.3)
    time.sleep(0.1)
    # Simulate _mark_stale: bumps epoch + clears flags
    app._prep_epoch += 1
    app._preparing = False
    t1.join(timeout=2.0)
    assert app._next_prepared is None, "stale prep installed buffer with old cue"
    print(f"  PASS: stale-marked prep discards its (old-cue) buffer")


def test_enter_on_song_during_prep():
    """User presses Enter on a different song while Prepare is rendering â€” the
    in-flight prep was computing start_rate against the OLD now-playing BPM,
    so its buffer is wrong for the newly-loaded track. _do_load_now bumps the
    epoch; orphaned worker must discard its buffer."""
    app = FakeApp()
    t1, w1 = app.start_prep(0.3)
    time.sleep(0.1)
    # Simulate _do_load_now's epoch bump + flag clear
    app._prep_epoch += 1
    app._preparing = False
    app._prep_progress = 0.0
    t1.join(timeout=2.0)
    assert app._next_prepared is None, (
        "Enter-during-prep installed a wrong-now-BPM buffer"
    )
    assert w1["rejected"] > 0
    print(f"  PASS: Enter-during-prep discards orphaned (old-now-BPM) buffer")


if __name__ == "__main__":
    test_prep_completes_normally_when_no_bump()
    test_n_during_prep_drops_buffer()
    test_double_n_during_prep()
    test_mark_stale_during_prep()
    test_enter_on_song_during_prep()
    print("\nAll prep-epoch tests passed.")
