"""Verify NowPlayingPanel renders 2 lines without a phase, 3 lines with one."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from automix.app import NowPlayingPanel


def make_panel():
    p = NowPlayingPanel()
    last = {"text": ""}
    p.update = lambda s, *a, **k: last.__setitem__("text", s if isinstance(s, str) else str(s))
    return p, last


def test_no_phase():
    p, last = make_panel()
    p.set_track("/songs/A.mp3", bpm=120.0, key="C maj")
    p.refresh_progress(44100 * 30, 44100 * 180)
    text = last["text"]
    print("--- no phase ---")
    print(text)
    assert text.count("\n") == 1, "expected 2 lines"
    assert "0:30" in text and "3:00" in text


def test_with_phase():
    p, last = make_panel()
    p.set_track("/songs/A.mp3", bpm=120.0, key="C maj")
    p.set_phase("MIXING the two tracks - cannot mix another track")
    p.refresh_progress(44100 * 30, 44100 * 180)
    text = last["text"]
    print("\n--- with phase ---")
    print(text)
    assert text.count("\n") == 2, "expected 3 lines"
    assert "MIXING the two tracks" in text


def test_set_track_clears_phase():
    p, last = make_panel()
    p.set_phase("OLD PHASE")
    p.set_track("/songs/B.mp3", bpm=130.0, key="A min")
    p.refresh_progress(0, 44100 * 200)
    assert "OLD PHASE" not in last["text"], "set_track should reset phase"
    print("\nset_track clears stale phase: PASS")


if __name__ == "__main__":
    test_no_phase()
    test_with_phase()
    test_set_track_clears_phase()
    print("\nAll phase-banner tests passed.")
