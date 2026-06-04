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


def _crop_frames(frames: List[List[str]]) -> List[List[str]]:
    """Trim border rows/columns that are blank in EVERY frame.

    Cropping all frames by the same amount keeps an animation registered and
    equal-size (so the banner never jumps). A row/column is dropped only when it is
    blank across all frames. Returns the cropped frames in the same order.
    """
    frames = [[r for r in f] for f in frames]
    if not frames or not frames[0]:
        return frames
    width = max((len(r) for f in frames for r in f), default=0)
    frames = [[r.ljust(width) for r in f] for f in frames]
    n = len(frames[0])

    def row_blank(i):
        return all(not f[i].strip() for f in frames)

    def col_blank(j):
        return all(f[i][j] == " " for f in frames for i in range(n))

    top = 0
    while top < n and row_blank(top):
        top += 1
    bot = n
    while bot > top and row_blank(bot - 1):
        bot -= 1
    left = 0
    while left < width and col_blank(left):
        left += 1
    right = width
    while right > left and col_blank(right - 1):
        right -= 1
    return [[r[left:right] for r in f[top:bot]] for f in frames]


def _crop_grid(grid: List[str]) -> List[str]:
    """Single-frame crop (thin wrapper over the shared multi-frame crop)."""
    return _crop_frames([grid])[0]


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


def _frames() -> List[List[str]]:
    """The shared-cropped art frames (all equal-size). One entry for a static image."""
    return _crop_frames(banner_art.FRAMES)


def banner_lines(frame: int = 0) -> List[Text]:
    """Compose the wordmark + portrait into one styled ``Text`` per terminal row.

    Wordmark on the left, portrait on the right. The wordmark column is padded to
    a constant width on every row (cfonts rstrips its rows to differing lengths),
    so the portrait stays column-aligned. ``frame`` selects the animation frame
    (all frames are equal-size, so the banner geometry is identical for each).
    """
    frames = _frames()
    # No frames -> wordmark only (the portrait column is omitted entirely).
    portrait = halfblock_rows(frames[frame % len(frames)], banner_art.PALETTE) if frames else []
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
    """Header widget: AUTOMIX wordmark on the left, pixel portrait on the right.

    Auto-sizes its height to the rendered art, so swapping in art of a different
    pixel height (via scripts/pixart_image_integrator.py) needs no CSS change. When
    the baked art has multiple frames (``FRAME_MS > 0``), the portrait cycles through
    them on a timer to animate; a single frame renders static. A small clock readout
    ticks in the top-right corner (refreshed once a second).
    """

    _frame = 0

    def on_mount(self) -> None:
        self.styles.height = banner_height()
        self.set_interval(1.0, self.refresh)  # tick the clock
        frame_ms = getattr(banner_art, "FRAME_MS", 0)
        if frame_ms > 0 and len(banner_art.FRAMES) > 1:
            self.set_interval(frame_ms / 1000, self._advance)

    def _advance(self) -> None:
        self._frame = (self._frame + 1) % len(banner_art.FRAMES)
        self.refresh()

    def render(self) -> Text:  # type: ignore[override]
        lines = banner_lines(self._frame)
        _add_clock(lines, self.size.width)
        return Text("\n").join(lines)


# Convenience for the headless probe / callers that want the rendered height.
def banner_height() -> int:
    return len(banner_lines())
