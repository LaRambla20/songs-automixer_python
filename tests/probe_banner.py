"""Headless smoke test for the top banner art.

Exercises the pure rendering helpers (no Textual mount, no audio device): the
art data is well-formed, the half-block renderer halves the grid height, every
grid letter resolves in the palette, the wordmark gradient produces one row per
line, and the composed banner builds a Rich Text without raising (catches
markup/escaping and palette-key typos). Prints PASS.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

from rich.text import Text

from automix import banner_art
from automix.banner import (
    _crop_frames,
    _wordmark_rows,
    banner_height,
    banner_lines,
    halfblock_rows,
)
import pixart_image_integrator  # build-time generator; its colour helpers are pure


def test_art_data_wellformed():
    assert isinstance(banner_art.FRAME_MS, int) and banner_art.FRAME_MS >= 0
    assert " " not in banner_art.PALETTE, "space must stay reserved for background"
    if not banner_art.FRAMES:  # no-image / wordmark-only mode
        assert banner_art.FRAME_MS == 0 and not banner_art.PALETTE, "no-image art must be empty"
        print("PASS: art data well-formed (no image - wordmark only)")
        return
    for fi, grid in enumerate(banner_art.FRAMES):
        assert grid, f"frame {fi} is empty"
        w = len(grid[0])
        assert all(len(r) == w for r in grid), f"frame {fi} rows are ragged"
        used = {c for row in grid for c in row if c != " "}
        missing = used - set(banner_art.PALETTE)
        assert not missing, f"frame {fi} uses letters absent from PALETTE: {missing}"
    for letter, hexcol in banner_art.PALETTE.items():
        assert hexcol.startswith("#") and len(hexcol) == 7, (letter, hexcol)
    print(f"PASS: art data well-formed ({len(banner_art.FRAMES)} frame(s), FRAME_MS={banner_art.FRAME_MS})")


def test_frames_equal_size():
    if not banner_art.FRAMES:
        print("PASS: no frames (wordmark only)")
        return
    # All frames must crop to identical dimensions, or the animation would jump.
    cropped = _crop_frames(banner_art.FRAMES)
    h = len(cropped[0])
    w = len(cropped[0][0]) if cropped[0] else 0
    assert all(len(f) == h and all(len(r) == w for r in f) for f in cropped), "frames differ in size"
    print(f"PASS: all {len(cropped)} frame(s) equal-size after shared crop ({h}x{w})")


def test_crop_trims_blank_border():
    if not banner_art.FRAMES:
        print("PASS: no frames to crop (wordmark only)")
        return
    cropped = _crop_frames(banner_art.FRAMES)
    assert cropped and cropped[0], "crop removed everything"
    n, width = len(cropped[0]), len(cropped[0][0])
    # No border row/col is blank across ALL frames (shared crop is tight).
    assert not all(not f[0].strip() for f in cropped), "shared blank top row remains"
    assert not all(not f[-1].strip() for f in cropped), "shared blank bottom row remains"
    assert not all(f[i][0] == " " for f in cropped for i in range(n)), "shared blank left column remains"
    assert not all(f[i][width - 1] == " " for f in cropped for i in range(n)), "shared blank right column remains"
    print("PASS: shared crop trims blank borders")


def test_halfblock_halves_height():
    if not banner_art.FRAMES:  # wordmark-only: height is just the wordmark + 2
        assert banner_height() == len(_wordmark_rows()) + 2
        print("PASS: wordmark-only banner height")
        return
    # use the SHARED crop (what the renderer actually uses), not a single-frame crop
    cropped = _crop_frames(banner_art.FRAMES)[0]
    rows = halfblock_rows(cropped, banner_art.PALETTE)
    expected = (len(cropped) + 1) // 2
    assert len(rows) == expected, (len(rows), expected)
    assert all(isinstance(r, Text) for r in rows)
    # banner is 2 rows taller than the tallest of (portrait, wordmark)
    assert banner_height() == max(expected, len(_wordmark_rows())) + 2
    print(f"PASS: half-block render is {expected} rows tall")


def test_wordmark_rows_match():
    rows = _wordmark_rows()
    assert len(rows) == len(banner_art.WORDMARK)
    assert all(isinstance(r, Text) for r in rows)
    print("PASS: wordmark gradient rows")


def test_banner_lines_compose():
    h = banner_height()
    # base render (frame 0, or wordmark-only when there are no frames) always works
    base = banner_lines()
    assert base and all(isinstance(ln, Text) for ln in base) and len(base) == h
    assert Text("\n").join(base).plain
    # plus every animation frame composes to the same row count
    for fi in range(len(banner_art.FRAMES)):
        lines = banner_lines(fi)
        assert len(lines) == h, (fi, len(lines), h)
        assert Text("\n").join(lines).plain
    print(f"PASS: banner composes ({h} rows, {len(banner_art.FRAMES)} frame(s))")


def test_theme_palette_levels():
    # one hue, one level -> the full-brightness base
    assert pixart_image_integrator.theme_palette(["green"], 1) == ["#00ff41"]
    # N levels -> N entries, brightest first, darkest last
    pal = pixart_image_integrator.theme_palette(["green"], 5)
    assert len(pal) == 5 and pal[0] == "#00ff41"
    assert pal[-1] == "#002e0c"  # 0x00ff41 * DARKEST_LEVEL (0.18)
    # all four hues x levels
    assert len(pixart_image_integrator.theme_palette(["green", "cyan", "yellow", "magenta"], 5)) == 20
    print("PASS: theme_palette levels/brightness")


def test_nearest_color_picks_sensible_hue():
    bright = [pixart_image_integrator.HUES[h] for h in ("green", "cyan", "yellow", "magenta")]
    # pure red is closest to the magenta base among the four hues
    assert pixart_image_integrator.nearest_color((255, 0, 0), bright) == pixart_image_integrator.HUES["magenta"]
    # pure black snaps to a darkest-level entry (low brightness), not a bright hue
    full = [pixart_image_integrator.unhex(h) for h in pixart_image_integrator.theme_palette(list(pixart_image_integrator.HUES), 5)]
    near_black = pixart_image_integrator.nearest_color((0, 0, 0), full)
    assert max(near_black) <= 80, near_black
    print("PASS: nearest_color hue selection + dark retint")


if __name__ == "__main__":
    test_art_data_wellformed()
    test_frames_equal_size()
    test_crop_trims_blank_border()
    test_halfblock_halves_height()
    test_wordmark_rows_match()
    test_banner_lines_compose()
    test_theme_palette_levels()
    test_nearest_color_picks_sensible_hue()
    print("All banner probes passed")
