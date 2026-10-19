"""Analysis cache: location-independent keys, legacy-key migration, failures not
cached, atomic save. No audio needed -- analyze_file is stubbed, files are dummies."""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import automix.analyzer as an

calls = []


def fake_analyze(path):
    calls.append(path)
    if "broken" in os.path.basename(path):
        raise OSError("disk went away")
    return {"bpm": 120.0, "key": "C maj", "beats": [1], "downbeats": [1]}


def write(path, data=b"x" * 100, mtime=1_700_000_000.25):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    os.utime(path, (mtime, mtime))


def main():
    tmp = tempfile.mkdtemp()
    try:
        an.CACHE_PATH = Path(tmp) / "cache.json"
        an.analyze_file = fake_analyze

        # First scan analyzes; failure is returned empty but NOT cached.
        lib1 = os.path.join(tmp, "F", "Music")
        write(os.path.join(lib1, "house", "track.mp3"))
        write(os.path.join(lib1, "house", "broken.mp3"))
        res = an.analyze_library(lib1)
        assert len(calls) == 2
        assert res[os.path.join(lib1, "house", "broken.mp3")]["bpm"] == 0.0
        assert len(an._load_cache()) == 1, "failed analysis must not be cached"
        assert not (Path(tmp) / "cache.json.tmp").exists(), "temp file left behind"
        print("  PASS failure not cached; atomic save leaves no temp file")

        # Same file on a "different drive", different subfolder, different case.
        calls.clear()
        lib2 = os.path.join(tmp, "G", "Other")
        write(os.path.join(lib2, "sets", "TRACK.mp3"), mtime=1_700_000_000.9)
        res = an.analyze_library(lib2)
        assert calls == [], f"moved/re-cased file re-analyzed: {calls}"
        assert res[os.path.join(lib2, "sets", "TRACK.mp3")]["bpm"] == 120.0
        print("  PASS moved + re-cased + sub-second mtime drift -> cache hit")

        # Failed file is retried next launch.
        calls.clear()
        an.analyze_library(lib1)
        assert [os.path.basename(c) for c in calls] == ["broken.mp3"]
        print("  PASS failed file retried on next launch")

        # Different size (e.g. tag edit) -> re-analyzed.
        calls.clear()
        write(os.path.join(lib2, "sets", "TRACK.mp3"), data=b"y" * 101)
        an.analyze_library(lib2)
        assert len(calls) == 1
        print("  PASS size change -> re-analyzed")

        # Legacy path-based entry is migrated, not re-analyzed.
        calls.clear()
        old = os.path.join(tmp, "legacy", "sub", "old.mp3")
        write(old)
        legacy = an._legacy_file_key(old)
        with open(an.CACHE_PATH, "w") as f:
            json.dump({"version": an.CACHE_VERSION, "entries": {
                legacy: {"bpm": 99.0, "key": "A min", "beats": [], "downbeats": []}}}, f)
        res = an.analyze_library(os.path.join(tmp, "legacy"))
        assert calls == [] and res[old]["bpm"] == 99.0
        entries = an._load_cache()
        assert legacy not in entries and an._file_key(old) in entries
        print("  PASS legacy entry migrated to the new key")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\nAll cache tests passed.")


if __name__ == "__main__":
    main()
