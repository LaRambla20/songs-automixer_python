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
    _crop_grid,
    _wordmark_rows,
    banner_height,
    banner_lines,
    halfblock_rows,
)
import pixart_image_integrator  # build-time generator; its colour helpers are pure


def test_art_data_wellformed():
    assert banner_art.GRID, "GRID is empty"
    w = len(banner_art.GRID[0])
    assert all(len(r) == w for r in banner_art.GRID), "GRID rows are ragged"
    assert " " not in banner_art.PALETTE, "space must stay reserved for background"
    # every non-space letter in the grid resolves to a colour
    used = {c for row in banner_art.GRID for c in row if c != " "}
    missing = used - set(banner_art.PALETTE)
    assert not missing, f"grid uses letters absent from PALETTE: {missing}"
    # palette values are #rrggbb
    for letter, hexcol in banner_art.PALETTE.items():
        assert hexcol.startswith("#") and len(hexcol) == 7, (letter, hexcol)
    print("PASS: art data well-formed")


def test_crop_trims_blank_border():
    cropped = _crop_grid(banner_art.GRID)
    assert cropped, "crop removed everything"
    assert cropped[0].strip() and cropped[-1].strip(), "blank border rows remain"
    # no fully-blank edge columns
    assert any(r[0] != " " for r in cropped), "blank left column remains"
    assert any(r[-1] != " " for r in cropped), "blank right column remains"
    print("PASS: crop trims blank border")


def test_halfblock_halves_height():
    cropped = _crop_grid(banner_art.GRID)
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
    lines = banner_lines()
    assert lines and all(isinstance(ln, Text) for ln in lines)
    # the composed banner is at least as tall as the portrait
    assert len(lines) == banner_height()
    joined = Text("\n").join(lines)
    assert joined.plain  # renders to a plain string without raising
    print(f"PASS: banner composes ({len(lines)} rows)")


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
    test_crop_trims_blank_border()
    test_halfblock_halves_height()
    test_wordmark_rows_match()
    test_banner_lines_compose()
    test_theme_palette_levels()
    test_nearest_color_picks_sensible_hue()
    print("All banner probes passed")
