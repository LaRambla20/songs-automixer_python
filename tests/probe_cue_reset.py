"""Verify the fix: pressing N on a new song resets the cue (so a previous track's
cue can't get inherited via the snap), but pressing N on the same song preserves it."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from automix.app import NextTrackPanel
from automix.audio_engine import SAMPLE_RATE


def make_panel():
    """NextTrackPanel without a mounted Textual context â€” .update() is a no-op."""
    p = NextTrackPanel()
    p.update = lambda *a, **k: None   # bypass Rich rendering
    return p


def test_n_on_different_song_resets_cue():
    panel = make_panel()
    # Song A: downbeats at 1s, 3s, 5s, 7s (each at 44100 Hz)
    db_a = [SAMPLE_RATE * t for t in (1, 3, 5, 7)]
    panel.set_track("/songs/A.mp3", bpm=120.0, key="C maj", downbeats=db_a)
    # User typed cue 6.4s on song A â†’ snaps to 7s (nearest, 0.6 vs 1.4 to 5)
    panel.set_cue(6.4)
    assert abs(panel.cue - 7.0) < 0.01, f"expected cue ~7.0s, got {panel.cue}"
    assert panel.cue_snapped
    print(f"  Song A loaded, cue typed 6.4 -> snapped to {panel.cue:.2f}s [bar]")

    # Song B: downbeats at 0.5s, 2.5s, 4.5s. Pressing N on B should NOT snap
    # the 7.0 cue to song B's grid; it should reset cue to 0 â†’ snap to 0.5s.
    db_b = [int(SAMPLE_RATE * t) for t in (0.5, 2.5, 4.5)]
    panel.set_track("/songs/B.mp3", bpm=130.0, key="A min", downbeats=db_b)
    expected = 0.5
    assert abs(panel.cue - expected) < 0.01, (
        f"BUG: cue should reset to first downbeat ({expected}s), got {panel.cue}"
    )
    print(f"  Song B loaded with N -> cue reset, snapped to {panel.cue:.2f}s [bar]")
    print("  PASS: cue did NOT inherit from song A")


def test_re_n_on_same_song_keeps_cue():
    panel = make_panel()
    db = [SAMPLE_RATE * t for t in (1, 3, 5, 7)]
    panel.set_track("/songs/A.mp3", bpm=120.0, key="C maj", downbeats=db)
    panel.set_cue(4.0)   # snaps to 3 or 5 (nearest)
    cue_before = panel.cue
    print(f"  Song A loaded, cue at {cue_before:.2f}s")

    # Re-press N on the same path â†’ should keep the cue
    panel.set_track("/songs/A.mp3", bpm=120.0, key="C maj", downbeats=db)
    assert abs(panel.cue - cue_before) < 0.01, (
        f"cue changed on re-N for same song: {cue_before} -> {panel.cue}"
    )
    print(f"  Re-N on same song -> cue preserved at {panel.cue:.2f}s")
    print("  PASS: same-song re-queue keeps user cue")


def test_new_song_no_downbeats():
    panel = make_panel()
    panel.set_track("/songs/A.mp3", bpm=120.0, key="C maj",
                    downbeats=[SAMPLE_RATE * 2, SAMPLE_RATE * 4])
    panel.set_cue(3.0)
    assert panel.cue_snapped
    # Switch to a song with no downbeats â€” cue should reset to 0, no snap
    panel.set_track("/songs/B.mp3", bpm=0.0, key="?", downbeats=[])
    assert panel.cue == 0.0, f"expected 0.0, got {panel.cue}"
    assert not panel.cue_snapped
    print(f"  Switch to no-downbeats track -> cue={panel.cue:.2f}s, snapped={panel.cue_snapped}")
    print("  PASS: empty downbeats track gets cue=0, no bar lock")


if __name__ == "__main__":
    test_n_on_different_song_resets_cue()
    test_re_n_on_same_song_keeps_cue()
    test_new_song_no_downbeats()
    print("\nAll cue-reset tests passed.")
