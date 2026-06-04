"""The AutoMix top banner: a cfonts 'block' AUTOMIX wordmark beside a pixel-art
portrait, rendered with terminal half-blocks.

The art data (WORDMARK / PALETTE / GRID) lives in the generated ``banner_art``
module; this module owns only the rendering. The portrait uses the half-block
trick - one character cell stacks two vertical pixels: ``U+2580`` (upper half)
paints the top pixel in the foreground colour and the bottom pixel in the
background colour. Cells whose half is the transparent background are left
unpainted so the banner's own background shows through.

Everything is built as a Rich ``Text`` programmatically (no ``Static.update``
markup), so literal ``[``/``]`` in the art never hit Rich's markup parser.
"""
from __future__ import annotations

from datetime import datetime
from typing import List

from rich.style import Style
from rich.text import Text
from textual.widgets import Static

from . import banner_art

UPPER_HALF = "▀"  # the top pixel is fg, the bottom pixel is bg
LOWER_HALF = "▄"  # the bottom pixel is fg, the top pixel is bg

# Wordmark gradient: phosphor green at the top fading to a deep green at the
# bottom, matching the screen theme (#00ff41 -> #007722).
_WORD_TOP = (0x00, 0xFF, 0x41)
_WORD_BOT = (0x00, 0x77, 0x22)
# Gap between the portrait and the wordmark.
_GUTTER = "   "
# Dim green for the right-aligned clock readout.
_CLOCK_COLOR = "#00aa33"


def _crop_grid(grid: List[str]) -> List[str]:
    """Drop fully-blank border rows/columns so the portrait sits flush."""
    rows = [r for r in grid]
    while rows and not rows[0].strip():
        rows.pop(0)
    while rows and not rows[-1].strip():
        rows.pop()
    if not rows:
        return rows
    width = max(len(r) for r in rows)
    rows = [r.ljust(width) for r in rows]
    left = 0
    while left < width and all(r[left] == " " for r in rows):
        left += 1
    right = width
    while right > left and all(r[right - 1] == " " for r in rows):
        right -= 1
    return [r[left:right] for r in rows]


def halfblock_rows(grid: List[str], palette: dict) -> List[Text]:
    """Render a pixel grid to half its height in styled ``Text`` rows.

    Rows are taken in (top, bottom) pairs; an odd final row pairs with blanks.
    A space in the grid is the transparent background and is left unpainted.
    """
    rows = list(grid)
    if len(rows) % 2:
        rows.append(" " * (len(rows[0]) if rows else 0))
    width = max((len(r) for r in rows), default=0)
    out: List[Text] = []
    for i in range(0, len(rows), 2):
        top, bot = rows[i].ljust(width), rows[i + 1].ljust(width)
        line = Text()
        for tc, bc in zip(top, bot):
            top_fg = palette.get(tc) if tc != " " else None
            bot_fg = palette.get(bc) if bc != " " else None
            if top_fg is None and bot_fg is None:
                line.append(" ")
            elif top_fg is not None and bot_fg is None:
                line.append(UPPER_HALF, Style(color=top_fg))
            elif top_fg is None and bot_fg is not None:
                line.append(LOWER_HALF, Style(color=bot_fg))
            else:
                line.append(UPPER_HALF, Style(color=top_fg, bgcolor=bot_fg))
        out.append(line)
    return out


def _wordmark_rows() -> List[Text]:
    """The AUTOMIX block letters, one styled ``Text`` per row, top-down gradient."""
    rows = banner_art.WORDMARK
    n = max(len(rows) - 1, 1)
    out: List[Text] = []
    for i, line in enumerate(rows):
        t = i / n
        rgb = tuple(round(a + (b - a) * t) for a, b in zip(_WORD_TOP, _WORD_BOT))
        color = "#%02x%02x%02x" % rgb
        out.append(Text(line, Style(color=color, bold=True)))
    return out


def banner_lines() -> List[Text]:
    """Compose the wordmark + portrait into one styled ``Text`` per terminal row.

    Wordmark on the left, portrait on the right. The wordmark column is padded to
    a constant width on every row (cfonts rstrips its rows to differing lengths),
    so the portrait stays column-aligned. An empty ``GRID`` (the ``--no-image``
    mode) renders the wordmark alone.
    """
    grid = _crop_grid(banner_art.GRID)
    # No image -> wordmark only (the portrait column is omitted entirely).
    portrait = halfblock_rows(grid, banner_art.PALETTE) if grid else []
    words = _wordmark_rows()
    word_w = max((len(w.plain) for w in words), default=0)
    # Banner runs 2 rows taller than the (tallest) content - a blank row above and
    # below - with the wordmark and portrait each vertically centred in it.
    total = max(len(portrait), len(words)) + 2
    w_off = (total - len(words)) // 2
    p_off = (total - len(portrait)) // 2
    lines: List[Text] = []
    for i in range(total):
        line = Text()
        wi = i - w_off
        if 0 <= wi < len(words):
            line.append_text(words[wi])
            line.append(" " * (word_w - len(words[wi].plain)))
        else:
            line.append(" " * word_w)
        if portrait:
            line.append(_GUTTER)
            pi = i - p_off
            if 0 <= pi < len(portrait):
                line.append_text(portrait[pi])
        lines.append(line)
    return lines


def _add_clock(lines: List[Text], width: int) -> None:
    """Overlay a right-aligned ``HH:MM:SS`` clock on the banner's (blank) top row.

    Mutates ``lines[0]`` in place, padding it out to ``width`` so the time sits in
    the top-right corner. No-op when there is no room (very narrow terminal).
    """
    if not lines or width <= 0:
        return
    clock = datetime.now().strftime("%H:%M:%S")
    top = lines[0]
    pad = width - len(top.plain) - len(clock)
    if pad < 1:
        return
    top.append(" " * pad)
    top.append(clock, Style(color=_CLOCK_COLOR))


class Banner(Static):
    """Header widget: AUTOMIX wordmark on the left, static pixel portrait on the right.

    Auto-sizes its height to the rendered art, so swapping in art of a different
    pixel height (via scripts/pixart_image_integrator.py) needs no CSS change. A small
    clock readout ticks in the top-right corner (refreshed once a second).
    """

    def on_mount(self) -> None:
        self.styles.height = banner_height()
        self.set_interval(1.0, self.refresh)  # tick the clock

    def render(self) -> Text:  # type: ignore[override]
        lines = banner_lines()
        _add_clock(lines, self.size.width)
        return Text("\n").join(lines)


# Convenience for the headless probe / callers that want the rendered height.
def banner_height() -> int:
    return len(banner_lines())
