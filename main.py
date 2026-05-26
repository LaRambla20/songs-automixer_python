import sys
import os


RUBBERBAND_MISSING_MSG = """\
ERROR: rubberband CLI not found on PATH.

AutoMix renders smooth tempo transitions via the rubberband time-stretcher.

Install:
  Windows: choco install rubberband
           OR download from https://breakfastquay.com/rubberband/ and add to PATH
  Linux:   sudo apt install rubberband-cli
  macOS:   brew install rubberband
"""


def main():
    if len(sys.argv) < 2:
        print("Usage: python main.py <music_folder>")
        sys.exit(1)

    root = os.path.abspath(sys.argv[1])
    if not os.path.isdir(root):
        print(f"Error: '{root}' is not a directory")
        sys.exit(1)

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
    app = AutoMixApp(root, library)
    app.run()


if __name__ == "__main__":
    main()
