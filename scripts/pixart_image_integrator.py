"""Build-time generator for the AutoMix banner art (NOT a runtime dependency).

Reads a pixel-art PNG, samples it down to a small grid (sampling each cell centre), keys
out the background to transparent, optionally recolours every pixel into the UI's neon
palette, and writes the pure-data module ``automix/banner_art.py``
(WORDMARK + PALETTE + GRID + BACKGROUND).

Usually no flags beyond --recolor:

    pixart_image_integrator.py art.png --recolor          # static, neon (typical)
    pixart_image_integrator.py art.png                    # static, original colours
    pixart_image_integrator.py --no-image                 # wordmark only, no portrait
    pixart_image_integrator.py art.png --rows 9 --bg none # sizing / transparency knobs

With ``--grid auto`` (default) the grid is sized backwards from the target banner height
(``--rows`` character rows): the image is cropped to its content first, then sampled.
Transparency is handled automatically - a PNG with an alpha channel is keyed by its alpha
(transparent = background); otherwise the background is detected from the image's border
(when near-uniform) and keyed within a colour tolerance, clearing faint anti-aliasing halo.

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
# Non-hex sentinel that keyed background cells are set to (-> ' ' / transparent).
BG_MARK = "bg"
# Alpha below this counts as transparent (background) for PNGs with an alpha channel.
ALPHA_THRESH = 128


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
        # Take the border's MEDIAN colour and accept it as the background only if the
        # border is a tight cluster around it (within tol). This handles slightly
        # noisy / gradient backgrounds, where no single exact colour dominates.
        border = np.concatenate([arr[0], arr[-1], arr[:, 0], arr[:, -1]], axis=0)
        med = np.median(border, axis=0)
        brm = (border[:, 0] + med[0]) / 2.0
        bdr, bdg, bdb = border[:, 0] - med[0], border[:, 1] - med[1], border[:, 2] - med[2]
        bdist2 = (2 + brm / 256) * bdr * bdr + 4 * bdg * bdg + (2 + (255 - brm) / 256) * bdb * bdb
        if (bdist2 <= tol * tol).mean() < 0.9:
            return None, full  # border not a uniform cluster -> no reliable background
        bg = med
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


def has_alpha(img: Image.Image) -> bool:
    """True if the image carries real transparency (alpha channel or palette transparency)."""
    return img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)


def opaque_bbox(img: Image.Image, thresh: int = ALPHA_THRESH):
    """Bounding box of pixels with alpha >= ``thresh`` (the visible subject)."""
    a = np.asarray(img.convert("RGBA"))[..., 3]
    ys, xs = np.where(a >= thresh)
    if len(xs) == 0:
        w, h = img.size
        return (0, 0, w, h)
    return (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)


def sample_grid(img: Image.Image, grid_w: int, grid_h: int, key_alpha: bool = False) -> List[List[str]]:
    """Sample each cell centre to a hex colour. With ``key_alpha``, cells whose alpha is
    below ``ALPHA_THRESH`` become ``BG_MARK`` (transparent)."""
    src = img.convert("RGBA") if key_alpha else img.convert("RGB")
    w, h = src.size
    rows = []
    for gy in range(grid_h):
        row = []
        for gx in range(grid_w):
            sx = int((gx + 0.5) * w / grid_w)
            sy = int((gy + 0.5) * h / grid_h)
            px = src.getpixel((sx, sy))
            if key_alpha and px[3] < ALPHA_THRESH:
                row.append(BG_MARK)
            else:
                row.append(hexof(px))
        rows.append(row)
    return rows


def key_background(hexrows, bg_hex, tol):
    """Mark every sampled cell within ``tol`` of the background as ``BG_MARK`` (transparent)."""
    if bg_hex is None:
        return hexrows
    bg = unhex(bg_hex)
    tol2 = tol * tol
    return [[BG_MARK if redmean_sq(unhex(c), bg) <= tol2 else c for c in row] for row in hexrows]


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def build_data(hexrows, recolor_hexes):
    """Map a sampled grid -> ``(palette, grid)`` of palette letters.

    ``hexrows`` is a grid whose background cells are ``BG_MARK`` (-> ' '). ``recolor_hexes``
    is None (original colours, use SEMANTIC letters) or the theme palette to snap every
    non-background colour to its nearest entry.
    """
    flat = [c for row in hexrows for c in row]

    remap = {}  # original hex -> final hex (after optional recolour)
    if recolor_hexes is not None:
        pal_rgb = [unhex(h) for h in recolor_hexes]
        for hexcol in dict.fromkeys(flat):
            if hexcol == BG_MARK:
                continue
            remap[hexcol] = hexof(nearest_color(unhex(hexcol), pal_rgb))

    palette: dict = {}      # letter -> hex
    letter_of: dict = {BG_MARK: " "}
    seen_hex: dict = {}     # final hex -> letter (so collapsed colours share a letter)
    spare = iter(SPARE)
    for hexcol in dict.fromkeys(flat):
        if hexcol == BG_MARK:
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
    ap.add_argument("image", nargs="?", default=SRC,
                    help="source pixel-art PNG (default: assets/sample.png)")
    ap.add_argument("--out", default=OUT, help="output data module (default: automix/banner_art.py)")
    ap.add_argument("--grid", default="auto",
                    help="'auto' (size from --rows, default), a width 'N', or explicit 'WxH'")
    ap.add_argument("--rows", type=int, default=WORDMARK_ROWS,
                    help="target image height in character rows for --grid auto (default: wordmark height)")
    ap.add_argument("--bg", default="auto",
                    help="background: 'auto' (border colour), 'none', or '#rrggbb'")
    ap.add_argument("--bg-tolerance", type=float, default=BG_TOLERANCE,
                    help="redmean distance under which a colour is background (default: %g)" % BG_TOLERANCE)
    ap.add_argument("--no-image", action="store_true",
                    help="wordmark only - bake an empty portrait (no image in the banner)")
    ap.add_argument("--recolor", action="store_true", help="snap every pixel to the neon UI palette")
    ap.add_argument("--hues", default="green,cyan,yellow,magenta", help="comma list for --recolor (default: all four)")
    ap.add_argument("--levels", type=int, default=5, help="brightness levels per hue for --recolor (default: 5)")
    args = ap.parse_args(argv)

    if args.no_image:
        # Wordmark only: empty portrait, empty palette.
        write_module(args.out, 0, 0, {}, [], None, "RECOLOR:  n/a (no image - wordmark only).")
        print("wrote", args.out, "(no image - wordmark only)")
        return

    tol = args.bg_tolerance
    raw = str(args.grid).lower()

    recolor_hexes = None
    recolor_note = "RECOLOR:  off (original colours)."
    if args.recolor:
        hues = [h.strip() for h in args.hues.split(",") if h.strip()]
        bad = [h for h in hues if h not in HUES]
        if bad:
            ap.error("unknown hue(s): %s (choices: %s)" % (", ".join(bad), ", ".join(HUES)))
        recolor_hexes = theme_palette(hues, args.levels)
        recolor_note = "RECOLOR:  on - hues=%s levels=%d (neon UI palette)." % (",".join(hues), args.levels)

    img = Image.open(args.image)
    W, H = img.size

    # Choose the transparency mode: a PNG with alpha is keyed by alpha (when --bg auto);
    # otherwise the background colour is detected from the border (or forced via --bg).
    bg_arg = args.bg.lower()
    key_alpha = bg_arg == "auto" and has_alpha(img)
    if key_alpha:
        background, bbox = None, opaque_bbox(img)
        print("background: alpha-keyed  content bbox: %s" % (bbox,))
    else:
        background, bbox = detect_background(img, bg_arg, tol)
        print("background: %s  content bbox: %s" % (background, bbox))

    if raw == "auto":
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

    hexrows = sample_grid(src, grid_w, grid_h, key_alpha=key_alpha)
    if not key_alpha:
        hexrows = key_background(hexrows, background, tol)

    palette, grid = build_data(hexrows, recolor_hexes)
    write_module(args.out, grid_w, grid_h, palette, grid, background, recolor_note)

    print("wrote", args.out)
    print("palette letters:", "".join(palette))
    for row in grid:
        print(row)


if __name__ == "__main__":
    main()
