"""Headless smoke test for the top banner art.

Exercises the pure rendering helpers (no Textual mount, no audio device): the
art data is well-formed, the half-block renderer halves the grid height, every
grid letter resolves in the palette, the wordmark gradient produces one row per
line, the composed banner builds a Rich Text without raising (catches
markup/escaping and palette-key typos), the clock overlay right-aligns, and the
generator's alpha keying + recolour helpers behave. Prints PASS.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

from rich.text import Text

from automix import banner_art
from automix import __author__, __version__
from automix.banner import (
    _add_clock,
    _add_signature,
    _crop_grid,
    _wordmark_rows,
    banner_height,
    banner_lines,
    halfblock_rows,
)
import pixart_image_integrator  # build-time generator; its colour helpers are pure


def test_art_data_wellformed():
    assert " " not in banner_art.PALETTE, "space must stay reserved for background"
    if not banner_art.GRID:  # no-image / wordmark-only mode
        assert not banner_art.PALETTE, "no-image art must have an empty palette"
        print("PASS: art data well-formed (no image - wordmark only)")
        return
    w = len(banner_art.GRID[0])
    assert all(len(r) == w for r in banner_art.GRID), "GRID rows are ragged"
    used = {c for row in banner_art.GRID for c in row if c != " "}
    missing = used - set(banner_art.PALETTE)
    assert not missing, f"grid uses letters absent from PALETTE: {missing}"
    for letter, hexcol in banner_art.PALETTE.items():
        assert hexcol.startswith("#") and len(hexcol) == 7, (letter, hexcol)
    print("PASS: art data well-formed")


def test_crop_trims_blank_border():
    if not banner_art.GRID:
        print("PASS: no grid to crop (wordmark only)")
        return
    cropped = _crop_grid(banner_art.GRID)
    assert cropped and cropped[0].strip() and cropped[-1].strip(), "blank border rows remain"
    assert any(r[0] != " " for r in cropped), "blank left column remains"
    assert any(r[-1] != " " for r in cropped), "blank right column remains"
    print("PASS: crop trims blank border")


def test_halfblock_halves_height():
    if not banner_art.GRID:  # wordmark-only: height is just the wordmark + 2
        assert banner_height() == len(_wordmark_rows()) + 2
        print("PASS: wordmark-only banner height")
        return
    cropped = _crop_grid(banner_art.GRID)
    rows = halfblock_rows(cropped, banner_art.PALETTE)
    expected = (len(cropped) + 1) // 2
    assert len(rows) == expected and all(isinstance(r, Text) for r in rows)
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
    assert len(lines) == banner_height()
    assert Text("\n").join(lines).plain  # renders to a plain string without raising
    print(f"PASS: banner composes ({len(lines)} rows)")


def test_clock_overlay():
    import re
    lines = banner_lines()
    _add_clock(lines, 80)
    top = lines[0].plain
    assert len(top) == 80, len(top)
    assert re.search(r"\d\d:\d\d:\d\d$", top), repr(top[-12:])
    # too-narrow terminal: no room for the clock -> top row untouched
    lines2 = banner_lines()
    w = len(lines2[0].plain)
    _add_clock(lines2, w + 2)
    assert len(lines2[0].plain) == w
    print("PASS: clock overlay right-aligned (and no-ops when no room)")


def test_signature_overlay():
    lines = banner_lines()
    _add_signature(lines, 80)
    # single right-aligned bottom row: "<author> - v<version>", flush to width
    assert len(lines[-1].plain) == 80, len(lines[-1].plain)
    assert lines[-1].plain.rstrip().endswith("v" + __version__), repr(lines[-1].plain[-12:])
    assert __author__ in lines[-1].plain
    # too-narrow terminal: no room for the signature -> bottom row untouched
    lines2 = banner_lines()
    before = lines2[-1].plain
    _add_signature(lines2, 5)
    assert lines2[-1].plain == before
    print("PASS: signature overlay right-aligned (and no-ops when no room)")


def test_alpha_keying():
    from PIL import Image
    BG = pixart_image_integrator.BG_MARK
    # 2x1 RGBA: left fully opaque red, right fully transparent
    im = Image.new("RGBA", (2, 1), (0, 0, 0, 0))
    im.putpixel((0, 0), (255, 0, 0, 255))
    assert pixart_image_integrator.has_alpha(im)
    rows = pixart_image_integrator.sample_grid(im, 2, 1, key_alpha=True)
    assert rows[0][0] == "#ff0000" and rows[0][1] == BG, rows
    # opaque bbox excludes the transparent column
    assert pixart_image_integrator.opaque_bbox(im) == (0, 0, 1, 1)
    print("PASS: alpha keying (transparent -> background, opaque bbox)")


def test_theme_palette_levels():
    assert pixart_image_integrator.theme_palette(["green"], 1) == ["#00ff41"]
    pal = pixart_image_integrator.theme_palette(["green"], 5)
    assert len(pal) == 5 and pal[0] == "#00ff41"
    assert pal[-1] == "#002e0c"  # 0x00ff41 * DARKEST_LEVEL (0.18)
    assert len(pixart_image_integrator.theme_palette(["green", "cyan", "yellow", "magenta"], 5)) == 20
    print("PASS: theme_palette levels/brightness")


def test_nearest_color_picks_sensible_hue():
    bright = [pixart_image_integrator.HUES[h] for h in ("green", "cyan", "yellow", "magenta")]
    assert pixart_image_integrator.nearest_color((255, 0, 0), bright) == pixart_image_integrator.HUES["magenta"]
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
    test_clock_overlay()
    test_signature_overlay()
    test_alpha_keying()
    test_theme_palette_levels()
    test_nearest_color_picks_sensible_hue()
    print("All banner probes passed")
