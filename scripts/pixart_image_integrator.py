"""Build-time generator for the AutoMix banner art (NOT a runtime dependency).

Reads one or more pixel-art PNGs, samples each down to a small grid (sampling each
cell centre), keys out the background to transparent, optionally recolours every pixel
into the UI's neon palette, and writes the pure-data module ``automix/banner_art.py``
(WORDMARK + PALETTE + FRAMES + FRAME_MS + BACKGROUND).

ONE image = a static banner. SEVERAL images = an animation that cycles through them in
the given order every ``--frame-ms`` milliseconds. Usually no flags beyond --recolor:

    pixart_image_integrator.py art.png --recolor                       # static, neon (typical)
    pixart_image_integrator.py art.png                                 # static, original colours
    pixart_image_integrator.py a.png b.png c.png --recolor             # animation (1 s/frame)
    pixart_image_integrator.py a.png b.png --recolor --frame-ms 500    # animation, 0.5 s/frame
    pixart_image_integrator.py art.png --rows 9 --grid 32 --bg none    # sizing / transparency knobs

With ``--grid auto`` (default) the grid is sized backwards from the target banner height
(``--rows`` character rows). A single image is cropped to its content first; animation
frames are sampled at one common grid (full canvas) so they stay registered and equal
size. The background is detected from each image's border (when near-uniform) and keyed
out within a colour tolerance, which also clears the faint anti-aliasing halo.

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
# Non-hex sentinel that keyed background cells are set to, so a shared palette can
# be built across animation frames whose detected backgrounds differ slightly.
BG_MARK = "bg"
# Default frame interval (ms) for an animation (multiple images).
FRAME_MS = 1000


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
    """Mark every sampled cell within ``tol`` of the background as ``BG_MARK`` (transparent)."""
    if bg_hex is None:
        return hexrows
    bg = unhex(bg_hex)
    tol2 = tol * tol
    return [[BG_MARK if redmean_sq(unhex(c), bg) <= tol2 else c for c in row] for row in hexrows]


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def build_frames(frames, recolor_hexes):
    """Map sampled frames -> palette letters with ONE shared palette across all frames.

    ``frames`` is a list of hexrow grids whose background cells are ``BG_MARK``.
    Returns ``(palette, grids)`` where every frame's letters resolve in the same
    palette. ``recolor_hexes`` is None (original colours, use SEMANTIC letters) or
    the theme palette to snap every non-background colour to its nearest entry.
    """
    flat = [c for fr in frames for row in fr for c in row]

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
                "error: image(s) have more distinct colours than the %d available letters.\n"
                "Re-run with --recolor (snaps colours to the neon palette) or a smaller "
                "--rows / --grid." % len(SPARE)
            )
        letter_of[hexcol] = letter
        seen_hex[final] = letter
        palette[letter] = final

    grids = [["".join(letter_of[c] for c in row) for row in fr] for fr in frames]
    return palette, grids


def write_module(out_path, grid_w, grid_h, palette, grids, background, recolor_note, frame_ms):
    n = len(grids)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write('"""AutoMix banner art - GENERATED by scripts/pixart_image_integrator.py. Do not edit by hand.\n\n')
        f.write("WORDMARK: cfonts 'block' AUTOMIX rows (recoloured at render time).\n")
        f.write("PALETTE:  letter -> hex colour. ' ' (space) is the transparent background.\n")
        f.write("FRAMES:   %d frame(s), each %d rows x %d cols of palette letters; 2 px/cell via half-blocks.\n" % (n, grid_h, grid_w))
        f.write("FRAME_MS: ms per frame (0 = static single image; >0 = cycle the frames).\n")
        f.write("%s\n" % recolor_note)
        f.write('"""\n\n')
        f.write("WORDMARK = [\n")
        for ln in WORDMARK:
            f.write("    %r,\n" % ln)
        f.write("]\n\n")
        f.write("BACKGROUND = %r  # detected background -> transparent in the banner\n" % background)
        f.write("FRAME_MS = %d\n\n" % frame_ms)
        f.write("PALETTE = {\n")
        for letter, hexcol in palette.items():
            f.write("    %r: %r,\n" % (letter, hexcol))
        f.write("}\n\n")
        f.write("FRAMES = [\n")
        for grid in grids:
            f.write("    [\n")
            for row in grid:
                f.write("        %r,\n" % row)
            f.write("    ],\n")
        f.write("]\n")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Bake pixel art into automix/banner_art.py")
    ap.add_argument("images", nargs="*", default=[SRC],
                    help="source pixel-art PNG(s). One = static; several = animation cycling in "
                         "the given order (default: assets/sample.png)")
    ap.add_argument("--frame-ms", type=int, default=FRAME_MS,
                    help="ms per frame when several images are given (default: %d)" % FRAME_MS)
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
        # Wordmark only: empty portrait, empty palette, no animation.
        write_module(args.out, 0, 0, {}, [], None, "RECOLOR:  n/a (no image - wordmark only).", 0)
        print("wrote", args.out, "(no image - wordmark only)")
        return

    tol = args.bg_tolerance
    raw = str(args.grid).lower()

    # Recolour palette (shared across all frames).
    recolor_hexes = None
    recolor_note = "RECOLOR:  off (original colours)."
    if args.recolor:
        hues = [h.strip() for h in args.hues.split(",") if h.strip()]
        bad = [h for h in hues if h not in HUES]
        if bad:
            ap.error("unknown hue(s): %s (choices: %s)" % (", ".join(bad), ", ".join(HUES)))
        recolor_hexes = theme_palette(hues, args.levels)
        recolor_note = "RECOLOR:  on - hues=%s levels=%d (neon UI palette)." % (",".join(hues), args.levels)

    images = list(args.images)
    frames_hexrows = []
    background = None

    if len(images) == 1:
        # Static single image: crop to content (auto) and sample to --rows.
        img = Image.open(images[0])
        W, H = img.size
        background, bbox = detect_background(img, args.bg.lower(), tol)
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
        frames_hexrows.append(key_background(sample_grid(src, grid_w, grid_h), background, tol))
        frame_ms = 0
    else:
        # Animation: crop every frame to the UNION of their content regions, then
        # sample to one common grid sized from --rows, so the SUBJECT fills the banner
        # height while frames stay registered + equal-size. The union is in RELATIVE
        # (fractional) coordinates because the frames can have different canvas sizes;
        # absolute pixel coords would misregister them (and run off smaller canvases).
        imgs = [Image.open(p) for p in images]
        det = [detect_background(im, args.bg.lower(), tol) for im in imgs]
        bgs = [d[0] for d in det]
        rels = []  # each content bbox as fractions of its own canvas
        for im, (_bg, bb) in zip(imgs, det):
            w, h = im.size
            rels.append((bb[0] / w, bb[1] / h, bb[2] / w, bb[3] / h))
        fx0 = min(r[0] for r in rels); fy0 = min(r[1] for r in rels)
        fx1 = max(r[2] for r in rels); fy1 = max(r[3] for r in rels)
        W0, H0 = imgs[0].size
        uw, uh = (fx1 - fx0) * W0, (fy1 - fy0) * H0  # union size on the first canvas
        if "x" in raw:
            grid_w, grid_h = (int(v) for v in raw.split("x", 1))
        elif raw == "auto":
            grid_h = max(1, args.rows * 2)
            grid_w = max(1, round(grid_h * uw / uh))
        else:
            grid_w = int(raw)
            grid_h = max(1, round(grid_w * uh / uw))
        background = bgs[0]
        print("grid: %dx%d  frames: %d  frame-ms: %d  union_frac=(%.2f,%.2f,%.2f,%.2f)"
              % (grid_w, grid_h, len(images), args.frame_ms, fx0, fy0, fx1, fy1))
        for p, im, bg in zip(images, imgs, bgs):
            w, h = im.size
            box = (round(fx0 * w), round(fy0 * h), round(fx1 * w), round(fy1 * h))
            frames_hexrows.append(key_background(sample_grid(im.crop(box), grid_w, grid_h), bg, tol))
            print("  %s  bg=%s  crop=%s" % (p, bg, box))
        frame_ms = args.frame_ms

    palette, grids = build_frames(frames_hexrows, recolor_hexes)
    write_module(args.out, grid_w, grid_h, palette, grids, background, recolor_note, frame_ms)

    print("wrote", args.out)
    print("palette letters:", "".join(palette))
    for i, grid in enumerate(grids):
        if len(grids) > 1:
            print("--- frame %d ---" % i)
        for row in grid:
            print(row)


if __name__ == "__main__":
    main()
