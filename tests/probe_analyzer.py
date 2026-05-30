"""Smoke test the new analyzer.analyze_file on one real track."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from automix.analyzer import analyze_file, ENGINE_SAMPLE_RATE

root = Path(__file__).parent.parent / "music"
paths = [p for p in root.rglob("*.mp3")][:2]
if not paths:
    print("no mp3 under ./music"); sys.exit(1)

for path in paths:
    print(f"\n=== {path.name} ===")
    t0 = time.time()
    rec = analyze_file(str(path))
    dt = time.time() - t0
    n_beats = len(rec["beats"])
    n_dbs = len(rec["downbeats"])
    print(f"  bpm={rec['bpm']}  key={rec['key']}  beats={n_beats}  downbeats={n_dbs}  ({dt:.1f}s)")
    if n_beats:
        first_beats_sec = [b / ENGINE_SAMPLE_RATE for b in rec["beats"][:6]]
        print(f"  first 6 beat times: {[f'{t:.3f}' for t in first_beats_sec]}")
    if n_dbs >= 2:
        first_dbs_sec = [d / ENGINE_SAMPLE_RATE for d in rec["downbeats"][:4]]
        print(f"  first 4 downbeat times: {[f'{t:.3f}' for t in first_dbs_sec]}")
        # bar length sanity
        spans = [rec["downbeats"][i+1] - rec["downbeats"][i] for i in range(min(5, n_dbs-1))]
        spans_sec = [s / ENGINE_SAMPLE_RATE for s in spans]
        expected_bar = 4 * 60.0 / max(rec["bpm"], 1)
        print(f"  downbeat spans (sec): {[f'{s:.3f}' for s in spans_sec]}  (expected ~{expected_bar:.3f}s)")
    # cache structure check
    assert "bpm" in rec and "key" in rec and "beats" in rec and "downbeats" in rec
print("\nAll analyzer record-field checks passed.")
