import sys
import os
import argparse


RUBBERBAND_MISSING_MSG = """\
ERROR: rubberband CLI not found on PATH.

AutoMix renders smooth tempo transitions via the rubberband time-stretcher.

Install:
  Windows: choco install rubberband
           OR download from https://breakfastquay.com/rubberband/ and add to PATH
  Linux:   sudo apt install rubberband-cli
  macOS:   brew install rubberband
"""

# Backspin transition SFX shipped with the repo, under samples/.
DEFAULT_BACKSPIN_SAMPLE = "top_DJ_Rewind_SFX_10.mp3"


def _resolve_backspin_sample(value: str) -> str:
    """A bare filename (no path separators) is looked up inside the bundled
    samples/ folder; an explicit relative/absolute path is used as-is."""
    if os.path.dirname(value):
        return os.path.abspath(value)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples", value)


def main():
    parser = argparse.ArgumentParser(
        prog="main.py", description="AutoMix - terminal DJ auto-mixer"
    )
    parser.add_argument("music_folder", help="Folder of audio files to browse and mix")
    parser.add_argument(
        "--backspin",
        default=DEFAULT_BACKSPIN_SAMPLE,
        metavar="SAMPLE",
        help=(
            "Backspin/rewind SFX one-shot for the B transition. A bare filename is "
            "resolved inside samples/; a path is used as-is. "
            f"(default: samples/{DEFAULT_BACKSPIN_SAMPLE})"
        ),
    )
    args = parser.parse_args()

    root = os.path.abspath(args.music_folder)
    if not os.path.isdir(root):
        print(f"Error: '{root}' is not a directory")
        sys.exit(1)

    backspin_sample = _resolve_backspin_sample(args.backspin)
    if not os.path.isfile(backspin_sample):
        sys.stderr.write(
            f"ERROR: backspin sample not found: {backspin_sample}\n"
            "Pass an existing file with --backspin <path>, or place one in samples/.\n"
        )
        sys.exit(2)

    from automix.stretcher import rubberband_available
    if not rubberband_available():
        sys.stderr.write(RUBBERBAND_MISSING_MSG)
        sys.exit(2)

    from automix.analyzer import scan_folder, analyze_library

    files = scan_folder(root)
    if not files:
        print("No audio files found in the specified folder.")
        sys.exit(1)

    total = len(files)
    print(f"AutoMix - found {total} audio file(s) under {root}")
    print("Analyzing (cached results load instantly)...\n")

    def progress(done: int, tot: int):
        pct = int(done / tot * 40) if tot > 0 else 0
        bar = "#" * pct + "-" * (40 - pct)
        print(f"\r  [{bar}]  {done}/{tot}", end="", flush=True)

    library = analyze_library(root, progress_callback=progress)
    print(f"\r  Analysis complete - {len(library)} tracks ready.          \n")

    from automix.app import AutoMixApp
    app = AutoMixApp(root, library, backspin_sample=backspin_sample)
    app.run()


if __name__ == "__main__":
    main()
