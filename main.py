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


def _resolve_output_device(substring: str) -> int:
    """Resolve a device name substring to a sounddevice index by case-insensitive
    match against OUTPUT-capable devices only (max_output_channels > 0). A headset's
    mono mic profile shares the device name but has 0 output channels, so filtering
    on output capability is essential. Returns the first match. Raises ValueError
    (caught in main) if nothing matches. Shared by --main-device and --headphones-device."""
    import sounddevice as sd
    needle = substring.lower()
    matches = [
        (i, d["name"])
        for i, d in enumerate(sd.query_devices())
        if d["max_output_channels"] > 0 and needle in d["name"].lower()
    ]
    if not matches:
        raise ValueError(
            f"no output device matches '{substring}'. Available output devices:\n  "
            + "\n  ".join(_output_device_lines())
        )
    return matches[0][0]


def _output_device_lines() -> list:
    """One 'index  name  (sr Hz)' line per output-capable device, for --list-devices
    and resolver error messages."""
    import sounddevice as sd
    lines = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_output_channels"] > 0:
            lines.append(f"{i:>3}  {d['name']}  ({int(d['default_samplerate'])} Hz)")
    return lines


def main():
    parser = argparse.ArgumentParser(
        prog="main.py", description="AutoMix - terminal DJ auto-mixer"
    )
    parser.add_argument(
        "music_folder",
        nargs="?",
        help="Folder of audio files to browse and mix (not required with --list-devices)",
    )
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
    parser.add_argument(
        "--main-device",
        default=None,
        metavar="NAME",
        help=(
            "Output device (name substring) for the MASTER mix. Pins playback to a "
            "chosen device instead of the OS default - e.g. the speakers, or a "
            "mixer/PA via the built-in codec's AUX (3.5mm) jack. Omit to follow the "
            "OS default output."
        ),
    )
    parser.add_argument(
        "--headphones-device",
        default=None,
        metavar="NAME",
        help=(
            "Output device (name substring) for pre-listening the NEXT track in "
            "headphones while the master mix plays elsewhere (PFL/cue, key L). "
            "Must be a SEPARATE OS output device - USB-C or Bluetooth headphones, "
            "NOT the built-in 3.5mm jack (which shares the master/speaker codec). "
            "Omit to disable cueing."
        ),
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="List output-capable audio devices (for --main-device / --headphones-device) and exit.",
    )
    args = parser.parse_args()

    if args.list_devices:
        print("Output-capable audio devices:")
        for line in _output_device_lines():
            print("  " + line)
        sys.exit(0)

    if args.music_folder is None:
        parser.error("the following arguments are required: music_folder")

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

    main_device = None
    if args.main_device is not None:
        try:
            main_device = _resolve_output_device(args.main_device)
        except ValueError as exc:
            sys.stderr.write(f"ERROR: --main-device {exc}\n")
            sys.exit(2)

    cue_device = None
    if args.headphones_device is not None:
        try:
            cue_device = _resolve_output_device(args.headphones_device)
        except ValueError as exc:
            sys.stderr.write(f"ERROR: --headphones-device {exc}\n")
            sys.exit(2)

    # Guard against the master and the headphone cue landing on the SAME output
    # (the symptom that prompted this: everything in the headphones). Compare the
    # cue device against the EFFECTIVE master device — the explicit --main-device,
    # or the OS default output when it's omitted (the case the old both-explicit
    # guard missed).
    if cue_device is not None:
        import sounddevice as sd
        effective_main = main_device
        main_was_default = False
        if effective_main is None:
            try:
                default_out = sd.default.device[1]
                if isinstance(default_out, int) and default_out >= 0:
                    effective_main = default_out
                    main_was_default = True
            except Exception:
                effective_main = None  # can't determine default → skip the check
        if effective_main is not None and effective_main == cue_device:
            name = sd.query_devices(cue_device)["name"]
            if main_was_default:
                sys.stderr.write(
                    f"ERROR: the headphones device (#{cue_device} {name!r}) is also your "
                    "OS default output, so the master mix would play through it too. "
                    "Pass --main-device to send the master to a different output (e.g. your "
                    "speakers/mixer), or change the Windows default output device.\n"
                )
            else:
                sys.stderr.write(
                    f"ERROR: --main-device and --headphones-device both resolve to the same "
                    f"device (#{cue_device} {name!r}). Point them at two different outputs - "
                    "e.g. --main-device to the speakers/mixer and --headphones-device to the "
                    "headphones.\n"
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
    app = AutoMixApp(
        root, library, backspin_sample=backspin_sample,
        cue_device=cue_device, main_device=main_device,
    )
    app.run()


if __name__ == "__main__":
    main()
