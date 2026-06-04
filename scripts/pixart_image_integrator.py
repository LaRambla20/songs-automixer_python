"""Build-time generator for the AutoMix banner art (NOT a runtime dependency).

Reads a pixel-art PNG, samples it down to a small grid (sampling each cell centre),
keys out the background to transparent, optionally recolours every pixel into the
UI's neon palette, maps each distinct colour to a short palette letter, and writes
the pure-data module ``automix/banner_art.py`` (WORDMARK + PALETTE + GRID + BACKGROUND).

Swap in any flat-colour pixel art - usually no flags beyond --recolor needed:

    .venv\\Scripts\\python.exe scripts\\pixart_image_integrator.py art.png --recolor   # neon theme (typical)
    .venv\\Scripts\\python.exe scripts\\pixart_image_integrator.py art.png             # original colours
    .venv\\Scripts\\python.exe scripts\\pixart_image_integrator.py art.png --rows 9    # taller in the banner
    .venv\\Scripts\\python.exe scripts\\pixart_image_integrator.py art.png --grid 32   # force a width
    .venv\\Scripts\\python.exe scripts\\pixart_image_integrator.py art.png --bg none   # no transparency

With ``--grid auto`` (default) the grid is sized backwards from the target banner
height: the image is cropped to its content (the non-background region) and sampled
to ``--rows`` character rows, deriving the width from the content's aspect ratio.
The background is detected from the image border (when near-uniform) and keyed out
within a colour tolerance, which also clears the faint anti-aliasing halo.

Requires Pillow + numpy, used for this build step only. The output module is plain
strings/dicts, so the running app never imports either.
"""
from __future__ import annotations

import argparse
import os
from typing import List, Sequence, Tuple

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "assets", "sample.png")
OUT = os.path.join(ROOT, "automix", "banner_art.py")

# AUTOMIX in cfonts' "block" font (generated once via `npx cfonts "AUTOMIX"
# -f block -a left`, ANSI stripped). Recoloured at render time, so kept raw here.
WORDMARK = [
    r"  █████╗  ██╗   ██╗ ████████╗  ██████╗  ███╗   ███╗ ██╗ ██╗  ██╗",
    r" ██╔══██╗ ██║   ██║ ╚══██╔══╝ ██╔═══██╗ ████╗ ████║ ██║ ╚██╗██╔╝",
    r" ███████║ ██║   ██║    ██║    ██║   ██║ ██╔████╔██║ ██║  ╚███╔╝",
    r" ██╔══██║ ██║   ██║    ██║    ██║   ██║ ██║╚██╔╝██║ ██║  ██╔██╗",
    r" ██║  ██║ ╚██████╔╝    ██║    ╚██████╔╝ ██║ ╚═╝ ██║ ██║ ██╔╝ ██╗",
    r" ╚═╝  ╚═╝  ╚═════╝     ╚═╝     ╚═════╝  ╚═╝     ╚═╝ ╚═╝ ╚═╝  ╚═╝",
]
WORDMARK_ROWS = len(WORDMARK)  # default target height for the image (--rows)

# Semantic letters for the bundled sample image's known flat colours (used only
# when NOT recolouring). ' ' is reserved for the detected background (transparent).
# Any colour not listed here is assigned a spare letter automatically.
SEMANTIC = {
    "#000000": "H",  # black  - hair / pupils / nose / outline
    "#7da269": "S",  # green  - skin
    "#577149": "D",  # darker green - skin shadow
    "#5e7253": "d",  # green  - jaw / neck shadow
    "#42503a": "E",  # dark gray-green - eye sockets
    "#ff0000": "R",  # red    - eyes
    "#281b09": "M",  # brown  - mouth
}
SPARE = "abcefghijklnopqrstuvwxyz0123456789"

# Neon bases for the recolour palette, matching the UI theme. Each is scaled down
# in brightness to produce the "nuances" (same idea as the wordmark gradient).
HUES = {
    "green": (0x00, 0xFF, 0x41),
    "cyan": (0x00, 0xE5, 0xFF),
    "yellow": (0xF5, 0xD9, 0x0A),
    "magenta": (0xFF, 0x2B, 0xD6),
}
# Brightness multipliers from full down to near-black-but-tinted (mirrors the
# wordmark gradient's drop). The darkest level is where black source pixels land.
DARKEST_LEVEL = 0.18
# Default redmean distance under which a colour counts as "the background".
BG_TOLERANCE = 60.0


# ---------------------------------------------------------------------------
# Colour helpers (pure - importable without Pillow side effects)
# ---------------------------------------------------------------------------

def hexof(rgb: Sequence[int]) -> str:
    return "#%02x%02x%02x" % (int(rgb[0]), int(rgb[1]), int(rgb[2]))


def unhex(h: str) -> Tuple[int, int, int]:
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def redmean_sq(c1: Sequence[int], c2: Sequence[int]) -> float:
    """Squared 'redmean' weighted-RGB distance (cheap, more perceptual than raw RGB)."""
    r1, g1, b1 = c1
    r2, g2, b2 = c2
    rm = (r1 + r2) / 2.0
    dr, dg, db = r1 - r2, g1 - g2, b1 - b2
    return (2 + rm / 256) * dr * dr + 4 * dg * dg + (2 + (255 - rm) / 256) * db * db


def theme_palette(hues: Sequence[str], levels: int) -> List[str]:
    """All hue x brightness-level colours as hex, brightest first.

    ``levels`` evenly spaced multipliers from 1.0 down to ``DARKEST_LEVEL``.
    """
    levels = max(1, levels)
    if levels == 1:
        mults = [1.0]
    else:
        step = (1.0 - DARKEST_LEVEL) / (levels - 1)
        mults = [1.0 - step * i for i in range(levels)]
    out: List[str] = []
    for name in hues:
        base = HUES[name]
        for m in mults:
            out.append(hexof(tuple(round(c * m) for c in base)))
    return out


def nearest_color(rgb: Tuple[int, int, int], palette_rgb: Sequence[Tuple[int, int, int]]) -> Tuple[int, int, int]:
    """Closest palette colour by redmean distance."""
    best = palette_rgb[0]
    best_d = None
    for c in palette_rgb:
        d = redmean_sq(rgb, c)
        if best_d is None or d < best_d:
            best_d, best = d, c
    return best


# ---------------------------------------------------------------------------
# Background detection + image sampling
# ---------------------------------------------------------------------------

def detect_background(img: Image.Image, bg_arg: str, tol: float):
    """Return ``(bg_hex_or_None, bbox)`` for an image.

    ``bg_arg`` is 'auto' (use the border's dominant colour when the border is
    near-uniform), 'none', or '#rrggbb'. ``bbox`` is the bounding box of the
    non-background pixels (the content) - or the full image when there is no
    background. Pixels within ``tol`` redmean distance of the background count as
    background, which also clears anti-aliasing halo around the edges.
    """
    w, h = img.size
    full = (0, 0, w, h)
    if bg_arg == "none":
        return None, full

    arr = np.asarray(img.convert("RGB"), dtype=np.float64)
    if bg_arg == "auto":
        border = np.concatenate([arr[0], arr[-1], arr[:, 0], arr[:, -1]], axis=0)
        colors, counts = np.unique(border.astype(np.int64), axis=0, return_counts=True)
        top = int(counts.argmax())
        if counts[top] / counts.sum() < 0.6:
            return None, full  # border not uniform -> no reliable background
        bg = colors[top].astype(np.float64)
    else:
        bg = np.array(unhex(bg_arg), dtype=np.float64)

    rm = (arr[..., 0] + bg[0]) / 2.0
    dr, dg, db = arr[..., 0] - bg[0], arr[..., 1] - bg[1], arr[..., 2] - bg[2]
    dist2 = (2 + rm / 256) * dr * dr + 4 * dg * dg + (2 + (255 - rm) / 256) * db * db
    nonbg = dist2 > tol * tol

    bg_hex = hexof(tuple(int(c) for c in bg))
    if not nonbg.any():
        return bg_hex, full
    ys, xs = np.where(nonbg)
    bbox = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
    return bg_hex, bbox


def sample_grid(img: Image.Image, grid_w: int, grid_h: int) -> List[List[str]]:
    img = img.convert("RGB")
    w, h = img.size
    rows = []
    for gy in range(grid_h):
        row = []
        for gx in range(grid_w):
            sx = int((gx + 0.5) * w / grid_w)
            sy = int((gy + 0.5) * h / grid_h)
            row.append(hexof(img.getpixel((sx, sy))))
        rows.append(row)
    return rows


def key_background(hexrows, bg_hex, tol):
    """Snap every sampled cell within ``tol`` of the background to the canonical bg hex."""
    if bg_hex is None:
        return hexrows
    bg = unhex(bg_hex)
    tol2 = tol * tol
    return [[bg_hex if redmean_sq(unhex(c), bg) <= tol2 else c for c in row] for row in hexrows]


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def build_data(hexrows, background, recolor_hexes):
    """Map sampled colours -> palette letters. Returns ``(palette, grid)``.

    ``recolor_hexes`` is None (keep original colours, use SEMANTIC letters) or the
    theme palette to snap every non-background colour to its nearest entry.
    """
    flat = [c for row in hexrows for c in row]

    remap = {}  # original hex -> final hex (after optional recolour)
    if recolor_hexes is not None:
        pal_rgb = [unhex(h) for h in recolor_hexes]
        for hexcol in dict.fromkeys(flat):
            if hexcol == background:
                continue
            remap[hexcol] = hexof(nearest_color(unhex(hexcol), pal_rgb))

    palette: dict = {}      # letter -> hex
    letter_of: dict = {background: " "}
    seen_hex: dict = {}     # final hex -> letter (so collapsed colours share a letter)
    spare = iter(SPARE)
    for hexcol in dict.fromkeys(flat):
        if hexcol == background:
            continue
        final = remap.get(hexcol, hexcol)
        if final in seen_hex:
            letter_of[hexcol] = seen_hex[final]
            continue
        letter = None if recolor_hexes is not None else SEMANTIC.get(final)
        if letter is None:
            letter = next(spare, None)
        if letter is None:
            raise SystemExit(
                "error: image has more distinct colours than the %d available letters.\n"
                "Re-run with --recolor (snaps colours to the neon palette) or a smaller "
                "--rows / --grid." % len(SPARE)
            )
        letter_of[hexcol] = letter
        seen_hex[final] = letter
        palette[letter] = final

    grid = ["".join(letter_of[c] for c in row) for row in hexrows]
    return palette, grid


def write_module(out_path, grid_w, grid_h, palette, grid, background, recolor_note):
    with open(out_path, "w", encoding="utf-8") as f:
        f.write('"""AutoMix banner art - GENERATED by scripts/pixart_image_integrator.py. Do not edit by hand.\n\n')
        f.write("WORDMARK: cfonts 'block' AUTOMIX rows (recoloured at render time).\n")
        f.write("PALETTE:  letter -> hex colour. ' ' (space) is the transparent background.\n")
        f.write("GRID:     %d rows x %d cols of palette letters; rendered 2 px/cell via half-blocks.\n" % (grid_h, grid_w))
        f.write("%s\n" % recolor_note)
        f.write('"""\n\n')
        f.write("WORDMARK = [\n")
        for ln in WORDMARK:
            f.write("    %r,\n" % ln)
        f.write("]\n\n")
        f.write("BACKGROUND = %r  # detected background -> transparent in the banner\n\n" % background)
        f.write("PALETTE = {\n")
        for letter, hexcol in palette.items():
            f.write("    %r: %r,\n" % (letter, hexcol))
        f.write("}\n\n")
        f.write("GRID = [\n")
        for row in grid:
            f.write("    %r,\n" % row)
        f.write("]\n")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Bake pixel art into automix/banner_art.py")
    ap.add_argument("image", nargs="?", default=SRC, help="source pixel-art PNG (default: assets/sample.png)")
    ap.add_argument("--out", default=OUT, help="output data module (default: automix/banner_art.py)")
    ap.add_argument("--grid", default="auto",
                    help="'auto' (size from --rows, default), a width 'N', or explicit 'WxH'")
    ap.add_argument("--rows", type=int, default=WORDMARK_ROWS,
                    help="target image height in character rows for --grid auto (default: wordmark height)")
    ap.add_argument("--bg", default="auto",
                    help="background: 'auto' (border colour), 'none', or '#rrggbb'")
    ap.add_argument("--bg-tolerance", type=float, default=BG_TOLERANCE,
                    help="redmean distance under which a colour is background (default: %g)" % BG_TOLERANCE)
    ap.add_argument("--recolor", action="store_true", help="snap every pixel to the neon UI palette")
    ap.add_argument("--hues", default="green,cyan,yellow,magenta", help="comma list for --recolor (default: all four)")
    ap.add_argument("--levels", type=int, default=5, help="brightness levels per hue for --recolor (default: 5)")
    args = ap.parse_args(argv)

    img = Image.open(args.image)
    W, H = img.size
    tol = args.bg_tolerance

    # Background: detected from the border (or explicit/none), plus the content bbox.
    background, bbox = detect_background(img, args.bg.lower(), tol)
    print("background: %s  content bbox: %s" % (background, bbox))

    # Grid size.
    raw = str(args.grid).lower()
    if raw == "auto":
        # Size backwards from the banner: crop to content, sample to `--rows`
        # character rows (2 grid rows per row via half-blocks), width from aspect.
        src = img.crop(bbox)
        bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
        grid_h = max(1, args.rows * 2)
        grid_w = max(1, round(grid_h * bw / bh))
        print("grid: %dx%d (auto: %d char-rows, content %dx%d)" % (grid_w, grid_h, args.rows, bw, bh))
    elif "x" in raw:
        src = img
        grid_w, grid_h = (int(v) for v in raw.split("x", 1))
        print("grid: %dx%d (explicit)" % (grid_w, grid_h))
    else:
        src = img
        grid_w = int(raw)
        grid_h = max(1, round(grid_w * H / W))
        print("grid: %dx%d (width given, height from aspect)" % (grid_w, grid_h))

    hexrows = sample_grid(src, grid_w, grid_h)
    hexrows = key_background(hexrows, background, tol)

    recolor_hexes = None
    recolor_note = "RECOLOR:  off (original colours)."
    if args.recolor:
        hues = [h.strip() for h in args.hues.split(",") if h.strip()]
        bad = [h for h in hues if h not in HUES]
        if bad:
            ap.error("unknown hue(s): %s (choices: %s)" % (", ".join(bad), ", ".join(HUES)))
        recolor_hexes = theme_palette(hues, args.levels)
        recolor_note = "RECOLOR:  on - hues=%s levels=%d (neon UI palette)." % (",".join(hues), args.levels)

    palette, grid = build_data(hexrows, background, recolor_hexes)
    write_module(args.out, grid_w, grid_h, palette, grid, background, recolor_note)

    print("wrote", args.out)
    print("palette letters:", "".join(palette))
    for row in grid:
        print(row)


if __name__ == "__main__":
    main()
