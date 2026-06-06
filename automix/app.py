import os
import time
import threading
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
from rich.markup import escape as _escape
from textual.app import App, ComposeResult
from textual.message import Message
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual import events
from textual.widgets import DataTable, Footer, Label, Static, Tree
from textual.widgets.tree import TreeNode

from .banner import Banner
from .audio_engine import AudioEngine, State, SAMPLE_RATE
from .cue_player import CuePlayer
from .analyzer import SUPPORTED_EXTENSIONS, empty_record
from .stretcher import make_transition_buffer
from .transition import plan_transition, tempo_compatible, TransitionPlan

# Seek step for the cue/PFL preview ([ / ] keys).
CUE_SEEK_SECONDS = 5.0
# Volume step per keypress (-/+ master, 9/0 cue), as a fraction of full scale.
VOLUME_STEP = 0.05
# Auto/emergency mix: when armed, the playing track auto-mixes into the next one
# as it enters its final EMERGENCY_SECONDS. That window is also the crossfade
# budget (the fade is clamped to the audio actually left). EMERGENCY_MIN_FADE is
# the case-3 bar-align fallback: if waiting for Track A's downbeat would leave
# less than this much fade, skip alignment and fire immediately.
EMERGENCY_SECONDS = 10.0
EMERGENCY_MIN_FADE = 3.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _smootherstep(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return x * x * x * (x * (x * 6 - 15) + 10)


def _fmt_time(samples: int) -> str:
    secs = max(0, samples) // SAMPLE_RATE
    return f"{secs // 60}:{secs % 60:02d}"


def _progress_segment(position: int, duration: int, bar_len: int) -> str:
    """One "M:SS / M:SS  [####----]" readout. The leading bracket is escaped
    (\\[) because Textual runs Static.update() text through Rich markup — an
    unescaped "[#..." reads as a colour tag and the bar vanishes."""
    pct = position / duration if duration > 0 else 0.0
    filled = max(0, min(bar_len, int(pct * bar_len)))
    bar = "#" * filled + "-" * (bar_len - filled)
    return f"{_fmt_time(position)} / {_fmt_time(duration)}  \\[{bar}]"


# ---------------------------------------------------------------------------
# Sub-widgets
# ---------------------------------------------------------------------------

class NowPlayingPanel(Static):
    def __init__(self, **kwargs):
        super().__init__("NOW PLAYING: \\[no track loaded]", **kwargs)
        self._path: Optional[str] = None
        self._bpm: float = 0.0
        self._key: str = ""
        # Outgoing track name shown as "outgoing → incoming" alongside the
        # incoming one, ONLY while a crossfade is in progress; None for the
        # normal single-track display. Set via set_track(mix_from=...) when the
        # now-playing swap fires, cleared by clear_mix_from() at MIXING→PLAYING.
        self._mix_from: Optional[str] = None
        # Set by _tick to surface the current transition phase (MIXING vs
        # tempo-restoration vs idle). Drives the third panel line.
        self._phase: str = ""
        # Active master-FX label (e.g. "HPF 65%") shown inline on the header line,
        # or None when the FX gate is off. Set via set_fx().
        self._fx: Optional[str] = None
        # Auto/emergency-mix armed flag — drives the magenta "AUTO" chip on the
        # header line. Set via set_auto().
        self._auto: bool = False

    def set_track(self, path: str, bpm: float, key: str, mix_from: Optional[str] = None):
        self._path = path
        self._bpm = bpm
        self._key = key
        self._mix_from = mix_from
        self._phase = ""
        self.refresh_progress(0, 0)

    def set_phase(self, phase: str) -> None:
        self._phase = phase

    def set_fx(self, label: Optional[str]) -> None:
        self._fx = label

    def set_auto(self, on: bool) -> None:
        self._auto = on
        # Re-render the idle line right away so arming/disarming is visible even with
        # no track loaded (when _tick doesn't refresh the panel). With a track loaded
        # the next _tick repaints within 100 ms.
        if self._path is None:
            self.update(self._idle_text())

    def _auto_chip(self) -> str:
        # Deliberate raw Rich markup (like fx_str) — the colour is the banner cat's eye
        # hex (banner_art PALETTE 'b') so the chip echoes the artwork.
        return "  |  [#cb22aa]AUTO[/]" if self._auto else ""

    def _idle_text(self) -> str:
        return f"NOW PLAYING: \\[no track loaded]{self._auto_chip()}"

    def clear_mix_from(self) -> None:
        """Drop the outgoing-track name (crossfade finished) so the panel shows
        only the now-playing track again."""
        self._mix_from = None

    def refresh_progress(
        self,
        position: int,
        duration: int,
        current_bpm: Optional[float] = None,
        mix_position: Optional[int] = None,
        mix_duration: Optional[int] = None,
        master_vol: Optional[float] = None,
    ):
        if self._path is None:
            self.update(self._idle_text())
            return
        name = _escape(Path(self._path).name)
        if self._mix_from:
            name = f"{_escape(self._mix_from)} → {name}"
        bpm = current_bpm if current_bpm is not None else self._bpm
        if master_vol is not None:
            vol_str = f"  |  Vol {round(master_vol * 100)}%" + (" (boost)" if master_vol > 1.0 else "")
        else:
            vol_str = ""
        fx_str = f"  |  FX {self._fx}" if self._fx else ""
        auto_str = self._auto_chip()
        # During a crossfade, show two bars side by side — the outgoing track
        # (left of the arrow) and the incoming track (right) — mirroring the
        # "outgoing → incoming" name line. Otherwise a single full-width bar.
        if mix_duration:
            out_seg = _progress_segment(position, duration, 18)
            in_seg = _progress_segment(mix_position or 0, mix_duration, 18)
            time_line = f"  {out_seg}  →  {in_seg}"
        else:
            time_line = f"  {_progress_segment(position, duration, 32)}"
        lines = [
            f"NOW PLAYING: {name}  |  {bpm:.1f} BPM  {self._key}{vol_str}{fx_str}{auto_str}",
            time_line,
        ]
        if self._phase:
            lines.append(f"  {self._phase}")
        self.update("\n".join(lines))

    def clear(self):
        self._path = None
        self._mix_from = None
        self._phase = ""
        # Keep the AUTO chip if armed — Stop/track-end clears the track but not the
        # auto-mix arming.
        self.update(self._idle_text())


class NextTrackPanel(Static):
    def __init__(self, **kwargs):
        super().__init__("NEXT TRACK: \\[none]", **kwargs)
        self._path: Optional[str] = None
        self._bpm: float = 0.0
        self._key: str = ""
        self._cue: float = 0.0
        # Raw (unsnapped) cue: 0.0 or exactly what the user typed. The backspin
        # transition starts the next track from here; _cue holds the bar-snapped
        # version that the Prepare/Mix crossfade uses for bar alignment.
        self._cue_raw: float = 0.0
        self._fade: float = 16.0
        self._restore: float = 30.0
        self._status: str = ""
        self._display_bpm: Optional[float] = None
        # Per-track downbeat sample indices (at engine SAMPLE_RATE). When non-empty,
        # set_cue auto-snaps to the nearest one — this is what gives the mix its
        # bar alignment.
        self._downbeats: List[int] = []
        self._cue_snapped: bool = False
        # PFL pre-listen state, driven by _tick from the CuePlayer. When playing,
        # _render appends a live "♪ CUE  pos / dur  [bar]" line.
        self._cue_playing: bool = False
        self._cue_pos: float = 0.0
        self._cue_dur: float = 0.0
        self._cue_vol: float = 1.0

    def set_cue_state(self, playing: bool, pos: float, dur: float, vol: float = 1.0) -> None:
        self._cue_playing = playing
        self._cue_pos = pos
        self._cue_dur = dur
        self._cue_vol = vol
        self._render()

    def set_track(self, path: str, bpm: float, key: str, downbeats: Optional[List[int]] = None):
        # Cue points are track-specific; carrying the previous track's cue across
        # an N press would silently bias the new track's snap to whatever bar
        # happened to be close to the old position. Reset to 0 on every new track
        # (set_cue then snaps it to the first downbeat if any).
        path_changed = path != self._path
        self._path = path
        self._bpm = bpm
        self._key = key
        self._display_bpm = None
        self._downbeats = list(downbeats) if downbeats else []
        self._status = "\\[not prepared]"
        if path_changed:
            self.set_cue(0.0)
        else:
            # Re-snap from the user's raw cue (not the already-snapped value), so a
            # re-queue of the same song preserves the exact backspin start point.
            self.set_cue(self._cue_raw)

    def set_cue(self, seconds: float):
        target = max(0.0, seconds)
        self._cue_raw = target
        if self._downbeats:
            target_sample = int(target * SAMPLE_RATE)
            nearest = min(self._downbeats, key=lambda d: abs(d - target_sample))
            self._cue = nearest / SAMPLE_RATE
            self._cue_snapped = True
        else:
            self._cue = target
            self._cue_snapped = False
        self._render()

    def set_fade(self, seconds: float):
        self._fade = max(1.0, seconds)
        self._render()

    def set_restore(self, seconds: float):
        self._restore = max(1.0, seconds)
        self._render()

    def set_status(self, msg: str):
        self._status = msg
        self._render()

    def set_display_bpm(self, bpm: Optional[float]) -> None:
        self._display_bpm = bpm
        self._render()

    @property
    def cue(self) -> float:
        return self._cue

    @property
    def raw_cue(self) -> float:
        """Unsnapped cue (0.0 or user-typed). Used by the backspin transition."""
        return self._cue_raw

    @property
    def cue_snapped(self) -> bool:
        return self._cue_snapped

    @property
    def fade(self) -> float:
        return self._fade

    @property
    def restore(self) -> float:
        return self._restore

    def _render(self):
        if self._path is None:
            self.update("NEXT TRACK: \\[none]")
            return
        name = _escape(Path(self._path).name)
        raw_str = _fmt_time(int(self._cue_raw * SAMPLE_RATE))
        # Cue (raw) drives the backspin; Mix (bar-snapped) drives the crossfade. Only
        # show the Mix value when it actually snapped to a downbeat (otherwise it
        # equals the raw cue and the second field is just noise).
        if self._cue_snapped:
            mix_str = _fmt_time(int(self._cue * SAMPLE_RATE))
            cue_field = f"Cue: {raw_str}   Mix: {mix_str} \\[bar]"
        else:
            cue_field = f"Cue: {raw_str}"
        bpm_show = self._display_bpm if self._display_bpm is not None else self._bpm
        lines = [
            f"NEXT TRACK: {name}  |  {bpm_show:.1f} BPM  {self._key}",
            f"  {cue_field}  Fade: {self._fade:.0f}s  Restore: {self._restore:.0f}s  {self._status}",
            "  \\[C] Cue  \\[F] Fade  \\[R] Restore  \\[P] Prepare  \\[M] Mix  \\[B] Backspin  \\[L] Listen",
        ]
        if self._cue_playing:
            seg = _progress_segment(
                int(self._cue_pos * SAMPLE_RATE), int(self._cue_dur * SAMPLE_RATE), 18
            )
            lines.append(f"  ♪ CUE  {seg}  vol {round(self._cue_vol * 100)}%  (\\[ / ] seek)")
        self.update("\n".join(lines))

    def clear(self):
        self._path = None
        self._display_bpm = None
        self._downbeats = []
        self._cue_snapped = False
        self._cue_raw = 0.0
        self._cue_playing = False
        self.update("NEXT TRACK: \\[none]")


# ---------------------------------------------------------------------------
# Folder tree
# ---------------------------------------------------------------------------

def _collapse_subtree(node) -> None:
    """Collapse a node together with all of its expanded descendants.

    Textual's ``node.collapse()`` does NOT cascade, so collapsing an ancestor
    alone leaves hidden descendants still flagged expanded - their arrows would
    reappear as a stale 'v' (and their children spill open) the next time the
    ancestor is re-opened. Collapse depth-first (children before the node) so a
    re-expand always starts from a fully-collapsed subtree. Safe to call on the
    root (collapsing descendants only, plus the root itself if expandable)."""
    for child in node.children:
        if child.allow_expand and child.is_expanded:
            _collapse_subtree(child)
    if node.allow_expand and node.is_expanded:
        node.collapse()


class FolderTree(Tree):
    """Tree subclass that intercepts right-arrow to signal 'enter song browsing'."""

    class GoToSongs(Message):
        def __init__(self, folder: str) -> None:
            super().__init__()
            self.folder = folder

    def on_key(self, event: events.Key) -> None:
        if event.key == "right":
            node = self.cursor_node
            if node and node.data and isinstance(node.data, str) and os.path.isdir(node.data):
                event.prevent_default()
                event.stop()
                # Root just reveals its subfolders and keeps focus on the tree
                # (root display -> subfolder display), moving the highlight down
                # onto the first subfolder. A subfolder additionally loads its
                # songs and hands focus to the song panel.
                if node is self.root:
                    node.expand()
                    if node.children:
                        self.cursor_line = node.line + 1
                else:
                    # Accordion: collapse any other open sibling first so at most
                    # one subfolder is open at a time (mirror on_tree_node_selected).
                    parent = node.parent
                    if parent is not None:
                        for sib in parent.children:
                            if sib is not node and sib.allow_expand and sib.is_expanded:
                                _collapse_subtree(sib)
                    node.expand()
                    self.post_message(self.GoToSongs(node.data))
        elif event.key == "left":
            # Go up one display level in a single press: collapse the parent folder
            # and move the highlight onto it. From a first-level subfolder the parent
            # is the root, so one press lands back on the collapsed root (subfolder
            # display -> root display). This is the tree-side mirror of the DataTable
            # left-arrow that exits song browsing.
            node = self.cursor_node
            if node is None:
                return
            parent = node.parent
            if parent is not None and parent.line >= 0:
                event.prevent_default()
                event.stop()
                self.cursor_line = parent.line  # set before collapse: parent.line is stable
                _collapse_subtree(parent)
            elif node.allow_expand and node.is_expanded:
                # Fallback: an expanded node with no displayable parent (the root in
                # subfolder display) — collapse it in place.
                event.prevent_default()
                event.stop()
                _collapse_subtree(node)
        elif event.key == "up":
            # Once subfolders are shown, up/down navigate only among them — the root
            # is never re-highlighted by navigation. It returns to the highlight only
            # via the left-arrow collapse above. Block the step that would land on it.
            above = (
                self.get_node_at_line(self.cursor_line - 1)
                if self.cursor_line > 0
                else None
            )
            if above is self.root:
                event.prevent_default()
                event.stop()
        elif event.key == "space":
            # Tree's built-in space binding toggles node expansion, which would
            # flip the folder's arrow while browsing. Suppress that and route the
            # key to the app's global Play/Pause instead — node expand/collapse is
            # reserved for Enter / click / right-arrow. (prevent_default blocks the
            # Tree binding; we forward explicitly so space still pauses playback.)
            event.prevent_default()
            event.stop()
            self.app.action_toggle_pause()

    async def _on_click(self, event: events.Click) -> None:
        # A click on the expand arrow (the "toggle" caret) should do exactly what a
        # click on the folder name does: route every click through select_cursor so
        # both gestures hit the same on_tree_node_selected flow once. (auto_expand is
        # off, so select_cursor only posts NodeSelected; the app handler owns the
        # expand/collapse.)
        #
        # prevent_default()/stop() are REQUIRED: Textual dispatches _on_click at every
        # MRO level (FolderTree, Tree, Widget), so without suppressing the default the
        # inherited Tree._on_click ALSO fires — its arrow-toggle / second select_cursor
        # would immediately re-collapse the node we just opened (open-then-close flash).
        meta = event.style.meta
        if "line" in meta:
            event.prevent_default()
            event.stop()
            self.cursor_line = meta["line"]
            await self.run_action("select_cursor")

    def _render_line(self, y, x1, x2, base_style):
        # Highlight the root row while the mouse is over it, but never let that hover
        # cascade to the subfolders / indentation guides (the root is an ancestor of
        # every row, so Textual would otherwise highlight the whole tree). Drive it
        # from hover_line (a Tree reactive that persists) rather than the root's
        # transient _hover flag, which _invalidate()/_reset() clears on every
        # expand/collapse - that made the highlight vanish on click until the mouse
        # moved again. Write the backing field directly so we don't bump _updates
        # (which would defeat the per-line render cache); restore it after.
        root = self.root
        root_hovered = self.hover_line >= 0 and self.hover_line == root.line
        saved = root._hover_
        root._hover_ = root_hovered and y == root.line
        try:
            return super()._render_line(y, x1, x2, base_style)
        finally:
            root._hover_ = saved

    # Mouse wheel adjusts the active FX intensity *only while the gate is engaged*;
    # otherwise it falls through to the Tree's normal scrolling. Intercepting here
    # (rather than app-level) is required because a scrollable widget consumes the
    # scroll event before it can bubble to the App.
    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        if self.app._fx_wheel(-1):
            event.prevent_default()
            event.stop()

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        if self.app._fx_wheel(+1):
            event.prevent_default()
            event.stop()


class SongTable(DataTable):
    """DataTable subclass that diverts the mouse wheel to the FX intensity while the
    gate is engaged (same rationale as FolderTree); otherwise scrolls normally."""

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        if self.app._fx_wheel(-1):
            event.prevent_default()
            event.stop()

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        if self.app._fx_wheel(+1):
            event.prevent_default()
            event.stop()


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class AutoMixApp(App):
    CSS = """
    Screen {
        background: #0d0d0d;
        color: #00ff41;
    }
    #banner {
        background: #0d0d0d;
        color: #00ff41;
        padding: 0 1;
    }
    #browser {
        height: 1fr;
    }
    #folder-panel {
        width: 28;
        border: solid #00aa00;
        padding: 0 1;
    }
    #song-panel {
        width: 1fr;
        border: solid #00aa00;
        padding: 0 1;
    }
    #folder-label, #song-label {
        color: #00cc33;
        text-style: bold;
    }
    Tree {
        background: #0d0d0d;
        color: #00ff41;
        scrollbar-color: #005500;
    }
    #folder-tree {
        height: 1fr;
    }
    Tree > .tree--cursor {
        background: #003300;
        color: #00ff41;
    }
    Tree > .tree--guides-hover {
        color: $success-darken-3;
        text-style: none;
    }
    Tree > .tree--guides-selected {
        color: $success-darken-3;
        text-style: none;
    }
    DataTable {
        background: #0d0d0d;
        color: #00ff41;
        scrollbar-color: #005500;
    }
    #song-list {
        height: 1fr;
    }
    DataTable > .datatable--cursor {
        background: #003300;
        color: #00ff41;
    }
    DataTable > .datatable--hover {
        background: $boost;
    }
    DataTable > .datatable--header {
        background: #001a00;
        color: #00cc33;
        text-style: bold;
    }
    DataTable > .datatable--header-hover {
        background: #001a00;
    }
    #now-playing {
        border: solid #00cccc;
        color: #00cccc;
        height: 5;
        padding: 0 1;
    }
    #next-track {
        border: solid #cccc00;
        color: #cccc00;
        height: 6;
        padding: 0 1;
    }
    #status-bar {
        height: 1;
        background: #0d0d0d;
        color: #00aa33;
        padding: 0 1;
    }
    Footer {
        background: #0d0d0d;
        color: #00aa33;
    }
    Footer > .footer--key {
        background: #003300;
        color: #00aa33;
        text-style: bold;
    }
    Footer > .footer--description {
        color: #00aa33;
    }
    Footer > .footer--highlight {
        background: #003300;
        color: #00aa33;
    }
    Footer > .footer--highlight-key {
        background: $secondary 20%;
        color: #00aa33;
        text-style: bold;
    }
    """

    BINDINGS = [
        Binding("space", "toggle_pause", "Play/Pause", show=True),
        Binding("s", "stop", "Stop", show=True),
        Binding("n", "load_next", "-> Next", show=True),
        Binding("p", "prepare_mix", "Prepare", show=True),
        Binding("m", "mix_now", "Mix Now", show=True),
        Binding("b", "backspin", "Backspin", show=True),
        Binding("l", "cue_toggle", "Cue (PFL)", show=True),
        Binding("a", "toggle_auto", "Auto-Mix", show=True),
        Binding("g", "fx_gate", "FX Gate", show=True),
        Binding("c", "set_cue", "Set Cue", show=True),
        Binding("f", "set_fade", "Set Fade", show=True),
        Binding("r", "set_restore", "Restore", show=True),
        Binding("q", "quit", "Quit", show=True),
    ]

    def __init__(
        self,
        root_folder: str,
        library: Dict[str, Dict],
        backspin_sample: str,
        cue_device=None,
        main_device=None,
    ):
        super().__init__()
        self.root_folder = root_folder
        self.library = library

        # Pin the master output to an explicit device when given (speakers, or a
        # mixer via the AUX jack); None follows the OS default.
        self._main_device = main_device
        self.engine = AudioEngine(device=main_device)

        # Pre-listen ("PFL") cue: an independent second output stream on a
        # separate device (USB-C / Bluetooth headphones) for auditioning the
        # queued NEXT track while the master mix plays on the speakers. None when
        # --headphones-device was not passed (feature dormant) or the device failed
        # to open. Opened at mount so a bad device surfaces immediately.
        self._cue_device = cue_device
        self._cue: Optional[CuePlayer] = None
        # Mirrors _prep_epoch: bumped on every cue start/stop and whenever the
        # NEXT slot changes, so a stale decode worker discards its buffer instead
        # of auditioning a track that is no longer "next".
        self._cue_epoch: int = 0
        # True between the L keypress and the cue decode landing — lets a second L
        # during the decode read as "stop" rather than launching a second decode.
        self._cue_loading: bool = False
        # One-shot guard so a lost cue device only prints its error once.
        self._cue_dead_reported: bool = False

        # Backspin transition (B): the SFX one-shot is decoded once at mount into
        # _backspin_audio so the keypress has no decode hitch. None until loaded
        # (or if the decode failed, in which case action_backspin reports it).
        self._backspin_path = backspin_sample
        self._backspin_audio: Optional[np.ndarray] = None

        self._now_path: Optional[str] = None
        self._now_bpm: float = 0.0
        self._now_key: str = ""
        # Sample indices (at engine SAMPLE_RATE) where downbeats fall in the now-
        # playing track. Used by action_mix_now to pick a sample-accurate scheduled
        # crossfade start. Empty list = no alignment data → fall back to immediate mix.
        self._now_downbeats: List[int] = []

        self._next_path: Optional[str] = None
        self._next_bpm: float = 0.0
        self._next_key: str = ""
        self._next_downbeats: List[int] = []
        self._next_prepared: Optional[np.ndarray] = None
        # The skip-vs-stretch plan for the queued next track, computed at Prepare
        # time. Held alongside _next_prepared and reset in lockstep with it; a
        # skip plan means _next_prepared is the raw cue audio (no rubberband) and
        # action_mix_now must NOT arm a restoration ramp.
        self._next_plan: Optional[TransitionPlan] = None
        self._preparing: bool = False
        # True between Mix-pressed and the scheduled crossfade actually firing;
        # _tick uses it to update the status line when state flips PLAYING→MIXING.
        self._mix_scheduled: bool = False
        # For a deferred (bar-aligned) mix, the now-playing metadata swap is held
        # here until the crossfade actually fires, so the NowPlaying panel keeps
        # showing the outgoing track during the bar-wait instead of jumping early.
        # Tuple of (path, bpm, key, downbeats) or None.
        self._pending_now_swap: Optional[tuple] = None
        # Wall-clock timestamp of the previous _tick, used to freeze the
        # restoration BPM ramp while the engine is paused.
        self._last_tick_t: float = time.time()

        self._songs_in_view: List[str] = []

        # lightweight inline input (cue / fade / restore)
        self._input_mode: str = ""
        self._input_buf: str = ""

        # App-side mirror of the engine FX gate, so the 100 ms _tick only pushes the
        # live tempo to the engine (a lock acquire) while the gate is actually engaged.
        # Kept in sync by _toggle_fx_gate / action_stop; the engine remains source of truth.
        self._fx_enabled: bool = False

        # Auto/emergency mix. _auto_armed is the user toggle (A); while armed, _tick's
        # detector mixes the next track in as the playing one enters its final
        # EMERGENCY_SECONDS. _emergency_fired is a per-track one-shot so the 100 ms
        # ticks during the window (and the decode latency) don't re-fire — it is reset
        # on every now-playing swap so each fresh track gets exactly one arming.
        self._auto_armed: bool = False
        self._emergency_fired: bool = False

        # BPM-display animation for the restoration ramp inside the precomputed buffer
        self._restore_from_bpm: float = 0.0
        self._restore_to_bpm: float = 0.0
        self._restore_seconds: float = 30.0
        self._t_restore_start: float = 0.0   # 0.0 = animation not armed / already done

        self._prev_engine_state: State = State.IDLE

        # prep animation (BPM display on the NextTrackPanel while Prepare is running).
        # _prep_progress is driven by rubberband's own stderr progress (0.0 → 1.0).
        self._prep_animating: bool = False
        self._prep_progress: float = 0.0
        self._prep_from_bpm: float = 0.0
        self._prep_to_bpm: float = 0.0
        # Bumped whenever the queued next-track changes or a new Prepare starts.
        # Each prep worker captures the value of this counter at launch time and
        # checks it on completion; if it has changed (user pressed N or re-pressed
        # P), the worker silently discards its buffer instead of installing a
        # stale prep for the now-current track.
        self._prep_epoch: int = 0

    # ------------------------------------------------------------------
    # Compose
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Banner(id="banner")
        with Horizontal(id="browser"):
            with Vertical(id="folder-panel"):
                yield Label("FOLDERS", id="folder-label")
                yield FolderTree(Path(self.root_folder).name, id="folder-tree")
            with Vertical(id="song-panel"):
                yield Label("SONGS  [Enter] Now Playing  [N] Next Track", id="song-label")
                yield SongTable(id="song-list")
        yield NowPlayingPanel(id="now-playing")
        yield NextTrackPanel(id="next-track")
        yield Static("", id="status-bar")
        yield Footer()

    # ------------------------------------------------------------------
    # Mount
    # ------------------------------------------------------------------

    def on_mount(self):
        self._setup_song_table()
        self._build_tree()
        # Song panel starts empty: songs load only when a subfolder is entered.
        self.query_one("#folder-tree", FolderTree).focus()
        self.set_interval(0.1, self._tick)
        self._preload_backspin()
        self._open_cue_device()

    def _open_cue_device(self):
        """Open the PFL cue stream if --headphones-device was supplied. Failure is
        non-fatal: the feature stays dormant and the L keypress reports it."""
        if self._cue_device is None:
            return
        try:
            self._cue = CuePlayer(self._cue_device)
        except Exception as exc:
            self._cue = None
            self._status(f"Cue device failed to open ({exc}); cueing disabled.")

    def _preload_backspin(self):
        """Decode the backspin SFX once in the background so the B keypress is
        instant. Leaves _backspin_audio as None on failure; action_backspin then
        reports the sample didn't load."""
        def _work():
            try:
                self._backspin_audio = self.engine.load_audio(self._backspin_path)
            except Exception:
                self._backspin_audio = None
        threading.Thread(target=_work, daemon=True).start()

    def _setup_song_table(self):
        t = self.query_one("#song-list", DataTable)
        # Leading one-glyph gutter: ":)" marks songs tempo-compatible with the
        # now-playing track (octave-folded). Keep its column key to update cells
        # in place when the now-playing track changes.
        col_keys = t.add_columns("Mix", "Filename", "BPM", "Key")
        self._match_col_key = col_keys[0]
        t.cursor_type = "row"

    def _build_tree(self):
        tree = self.query_one("#folder-tree", Tree)
        # Drive expansion from the navigation handlers (so Enter/click don't
        # auto-toggle), and open on the "root display" with the root collapsed —
        # the user expands into the subfolder display, then into song browsing.
        tree.auto_expand = False
        tree.root.data = self.root_folder
        self._populate_node(tree.root, self.root_folder)
        tree.cursor_line = 0

    def _populate_node(self, node: TreeNode, folder: str):
        try:
            entries = sorted(os.listdir(folder))
        except PermissionError:
            return
        for entry in entries:
            full = os.path.join(folder, entry)
            if os.path.isdir(full):
                child = node.add(entry, data=full)
                # lazy placeholder so expand arrow appears
                try:
                    has_sub = any(
                        os.path.isdir(os.path.join(full, e))
                        for e in os.listdir(full)
                    )
                    if has_sub:
                        child.add_leaf("...", data="__placeholder__")
                except PermissionError:
                    pass

    # ------------------------------------------------------------------
    # Tree events
    # ------------------------------------------------------------------

    def on_tree_node_expanded(self, event: Tree.NodeExpanded):
        node = event.node
        if not node.data or not isinstance(node.data, str):
            return
        if not os.path.isdir(node.data):
            return
        children_data = [c.data for c in node.children]
        if "__placeholder__" in children_data:
            node.remove_children()
            self._populate_node(node, node.data)

    def _exit_song_browsing(self, node=None) -> None:
        """Clear the song list and collapse the folder we were browsing (its
        arrow returns to '>'), returning focus to the tree. Shared by the
        left-arrow exit and the second-activation toggle in
        on_tree_node_selected. Never collapses the root here."""
        table = self.query_one("#song-list", DataTable)
        table.clear()
        self._songs_in_view = []
        tree = self.query_one("#folder-tree", FolderTree)
        if node is None:
            node = tree.cursor_node
        if (
            node is not None
            and node is not tree.root
            and node.allow_expand
            and node.is_expanded
        ):
            _collapse_subtree(node)
        tree.focus()

    def on_tree_node_selected(self, event: Tree.NodeSelected):
        # Enter / mouse-click (auto_expand is off, so we drive expansion). Acts as
        # a toggle: a second activation on an already-open folder exits it. Opening
        # a subfolder collapses any other open sibling (accordion - one open at a
        # time), so the previously browsed folder's arrow returns to '>'. For the
        # Enter key this is a no-op in the common case (Enter moves focus to the
        # next level, so the same node can't be re-activated) and only toggles in
        # the edge cases where focus stays put (empty root / empty subfolder).
        node = event.node
        if not (node.data and isinstance(node.data, str) and os.path.isdir(node.data)):
            return
        tree = self.query_one("#folder-tree", FolderTree)

        if node.is_expanded:
            # Second activation -> exit.
            if node is tree.root:
                table = self.query_one("#song-list", DataTable)
                table.clear()
                self._songs_in_view = []
                # Collapse the whole subtree, not just the root: Textual's collapse
                # doesn't cascade, so collapsing only the root would leave open
                # descendants (down arrow / spilled children) when the root reopens.
                _collapse_subtree(node)  # back to root display
                tree.cursor_line = 0
                tree.focus()
            else:
                self._exit_song_browsing(node)
            return

        # First activation -> open.
        if node is tree.root:
            node.expand()
            # root display -> subfolder display: move the highlight onto the
            # first subfolder so the user is positioned to drill in.
            if node.children:
                tree.cursor_line = node.line + 1
            return

        # Subfolder: accordion - collapse any other open sibling first.
        parent = node.parent
        if parent is not None:
            for sib in parent.children:
                if sib is not node and sib.allow_expand and sib.is_expanded:
                    _collapse_subtree(sib)
        node.expand()
        self._load_songs_for(node.data)
        if self._songs_in_view:
            self.query_one("#song-list", DataTable).focus()
        # Empty subfolder: keep focus on the tree (don't jump into an empty table),
        # so a second Enter/click on the node collapses it.

    def on_folder_tree_go_to_songs(self, event: FolderTree.GoToSongs) -> None:
        self._load_songs_for(event.folder)
        # Don't jump focus into an empty table (mirror on_tree_node_selected); keep
        # focus on the tree so a song-less subfolder stays navigable/collapsible.
        if self._songs_in_view:
            self.query_one("#song-list", DataTable).focus()

    def _load_songs_for(self, folder: str):
        table = self.query_one("#song-list", DataTable)
        table.clear()
        self._songs_in_view = []

        try:
            entries = sorted(os.listdir(folder))
        except PermissionError:
            return

        for entry in entries:
            full = os.path.join(folder, entry)
            if os.path.isfile(full) and Path(entry).suffix.lower() in SUPPORTED_EXTENSIONS:
                rec = self.library.get(full, empty_record())
                bpm, key = rec["bpm"], rec["key"]
                bpm_str = f"{bpm:.1f}" if bpm > 0 else "---"
                table.add_row(self._match_marker(bpm), entry, bpm_str, key, key=full)
                self._songs_in_view.append(full)

    def _match_marker(self, song_bpm: float) -> str:
        """':)' when a song's tempo is mixable with the now-playing track
        (octave-folded), else blank. Blank when nothing is playing."""
        return ":)" if tempo_compatible(self._now_bpm, song_bpm) else ""

    def _refresh_match_markers(self) -> None:
        """Recompute the match gutter for every visible song against the current
        now-playing BPM. Called whenever the now-playing track changes."""
        table = self.query_one("#song-list", DataTable)
        for path in self._songs_in_view:
            bpm = self.library.get(path, empty_record())["bpm"]
            table.update_cell(path, self._match_col_key, self._match_marker(bpm))

    # ------------------------------------------------------------------
    # Song table events
    # ------------------------------------------------------------------

    def on_data_table_row_selected(self, event: DataTable.RowSelected):
        """Enter on a song row -> load as Now Playing.

        Only allowed when nothing is currently playing (engine IDLE: no track
        loaded yet, the prior track finished, or user pressed Stop). While a
        song is playing/paused/mixing/restoring, Enter is rejected with a clear
        status message — the user must queue with N and mix with M instead.
        """
        path = self._selected_song()
        if not path:
            return
        if self.engine.state != State.IDLE:
            self._status(
                "Cannot load: a song is already playing. "
                "Press N to queue this as the next track instead."
            )
            return
        self._do_load_now(path)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_toggle_pause(self):
        self.engine.pause()

    # ------------------------------------------------------------------
    # Master FX (HPF / LPF / Trans gate)
    # ------------------------------------------------------------------

    def action_fx_gate(self) -> None:
        """G binding: toggle the master FX gate (shown in the Footer)."""
        self._toggle_fx_gate()

    def _toggle_fx_gate(self) -> None:
        enabled, label = self.engine.fx_state()
        new_state = not enabled
        self.engine.set_fx_enabled(new_state)
        self._fx_enabled = new_state
        if new_state:
            # Keep the Trans gate synced to whatever is playing right now.
            self.engine.set_fx_tempo(self._now_bpm)
            self._status(f"FX gate ON  [{label}]  (1/2/3 pick effect, wheel = intensity)")
        else:
            self._status("FX gate OFF")
        self._refresh_fx_indicator()

    def _select_fx(self, fx_type: str) -> None:
        self.engine.set_fx_type(fx_type)
        enabled, label = self.engine.fx_state()
        state = "ON" if enabled else "off"
        self._status(f"FX effect: {label}  (gate {state})")
        self._refresh_fx_indicator()

    def _fx_wheel(self, direction: int) -> bool:
        """Mouse-wheel hook shared by the app, FolderTree and SongTable. Adjusts the
        selected FX intensity only while the gate is engaged; returns True if it
        consumed the scroll (so the caller can suppress normal list scrolling)."""
        # While typing an inline C/F/R value, leave the wheel alone (the keys are
        # already suppressed in that mode — keep the two consistent).
        if self._input_mode:
            return False
        if not self._fx_enabled:
            return False
        self.engine.adjust_fx(direction)
        _, label = self.engine.fx_state()
        self._status(f"FX: {label}")
        self._refresh_fx_indicator()
        return True

    def _refresh_fx_indicator(self) -> None:
        """Push the current FX label onto the NowPlaying panel (None when the gate is
        off, so the indicator disappears)."""
        enabled, label = self.engine.fx_state()
        panel = self.query_one("#now-playing", NowPlayingPanel)
        panel.set_fx(label if enabled else None)

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        # Reaches the app only over non-scrollable areas (the panels); the Tree and
        # SongTable handle the wheel themselves. Consume only when FX is engaged.
        if self._fx_wheel(-1):
            event.prevent_default()
            event.stop()

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        if self._fx_wheel(+1):
            event.prevent_default()
            event.stop()

    def action_stop(self):
        self.engine.stop()
        self._now_path = None
        self._now_bpm = 0.0
        self._now_key = ""
        self._now_downbeats = []
        self._t_restore_start = 0.0
        self._restore_from_bpm = 0.0
        self._restore_to_bpm = 0.0
        self._mix_scheduled = False
        self._pending_now_swap = None
        self._emergency_fired = False
        # Invalidate prep — both any in-flight worker (epoch bump) AND a completed
        # buffer (which was rendered against the now-stopped track's BPM). Without
        # clearing _next_prepared, a later Stop→Enter→M would mix a wrong-tempo
        # buffer. Keep the NEXT track queued but mark it unprepared.
        self._prep_epoch += 1
        self._preparing = False
        self._prep_animating = False
        self._prep_progress = 0.0
        self._next_prepared = None
        self._next_plan = None
        # Disengage the FX gate on Stop so the next track doesn't start mid-effect with
        # no on-screen indicator; the selected effect + intensities are kept for re-arming.
        self.engine.set_fx_enabled(False)
        self._fx_enabled = False
        self.query_one("#now-playing", NowPlayingPanel).clear()
        self._refresh_fx_indicator()
        self._refresh_match_markers()
        if self._next_path is not None:
            panel = self.query_one("#next-track", NextTrackPanel)
            panel.set_display_bpm(None)
            panel.set_status("\\[not prepared]")
        self._status("")

    def action_load_next(self):
        path = self._selected_song()
        if not path:
            return
        if path == self._next_path:
            self._status(f"{Path(path).name} is already queued as NEXT TRACK")
            return
        # The thing being auditioned is changing — stop the cue (Q7 lifecycle).
        self._stop_cue()
        rec = self.library.get(path, empty_record())
        bpm, key = rec["bpm"], rec["key"]
        # Bump epoch so any in-flight prep worker for the previous track will
        # detect it has been superseded and drop its buffer + progress updates.
        self._prep_epoch += 1
        self._next_path = path
        self._next_bpm = bpm
        self._next_key = key
        self._next_downbeats = list(rec["downbeats"])
        self._next_prepared = None
        self._next_plan = None
        self._preparing = False
        self._prep_animating = False
        self._prep_progress = 0.0
        panel = self.query_one("#next-track", NextTrackPanel)
        panel.set_display_bpm(None)
        panel.set_track(path, bpm, key, downbeats=self._next_downbeats)
        self._status(f"Queued as next: {Path(path).name}")

    def action_prepare_mix(self):
        if not self._next_path:
            self._status("No next track selected (press N on a song first)")
            return
        # A prepared buffer's start_rate = now_bpm / next_bpm; with nothing playing
        # (engine IDLE or _now_bpm == 0) that ratio is 0 and rubberband would render
        # the fade region frozen. Require a real track playing first.
        if self.engine.state == State.IDLE or self._now_bpm <= 0:
            self._status("Load a track first (Enter) before preparing a mix.")
            return
        if self._preparing:
            self._status("Already preparing...")
            return
        self._preparing = True
        # Bump and capture the epoch so this worker (and its progress callback)
        # can detect being superseded by a subsequent N or P press.
        self._prep_epoch += 1
        my_epoch = self._prep_epoch
        panel = self.query_one("#next-track", NextTrackPanel)
        now_bpm = self._now_bpm
        next_bpm = self._next_bpm
        next_path = self._next_path
        cue_sec = panel.cue
        fade_sec = panel.fade
        restore_sec = panel.restore

        # Decide skip-vs-stretch up front. A skip plan (near-identical tempos or
        # an exact half-/double-time pair) renders no rubberband at all.
        plan = plan_transition(now_bpm, next_bpm, fade_sec)

        if plan.skip:
            self._prepare_skip(panel, next_path, cue_sec, plan, my_epoch)
            return

        self._prep_animating = True
        self._prep_progress = 0.0
        self._prep_from_bpm = next_bpm
        self._prep_to_bpm = plan.matched_bpm
        panel.set_status(f"\\[PREPARING...] {next_bpm:.1f}→{plan.matched_bpm:.1f} BPM")
        self._status("Rendering transition with rubberband...")

        def _on_progress(p: float) -> None:
            # Drop progress updates from a superseded prep — otherwise an orphaned
            # worker pushes its monotonically-climbing progress onto whichever
            # track is now queued, scrambling that track's BPM animation.
            if self._prep_epoch != my_epoch:
                return
            # GIL-safe attribute write; the _tick() reader sees a consistent float.
            # Monotonic clamp: rubberband restarts the run from scratch when output
            # clipping is detected (printing a fresh "Pass 1: ... 0%" sequence), which
            # would otherwise yank the BPM display back to `next_bpm` mid-animation.
            # By refusing to ever decrease, the display plateaus during restarts
            # instead of oscillating.
            p = min(1.0, p)
            if p > self._prep_progress:
                self._prep_progress = p

        def _work():
            try:
                audio = self.engine.load_audio(next_path)
                cue_sample = int(cue_sec * SAMPLE_RATE)
                cue_audio = audio[cue_sample:]
                buf = make_transition_buffer(
                    cue_audio, plan.start_rate, fade_sec, restore_sec, SAMPLE_RATE,
                    progress_callback=_on_progress,
                )
                # Worker was superseded mid-render (user pressed N for a different
                # track, or re-pressed P). Discard the now-orphaned buffer rather
                # than installing it as the prep for the current next track.
                if self._prep_epoch != my_epoch:
                    return
                self._next_prepared = buf
                self._next_plan = plan
                self._preparing = False
                self.call_from_thread(self._on_prepared)
            except Exception as exc:
                if self._prep_epoch == my_epoch:
                    self._preparing = False
                    self.call_from_thread(self._status, f"Prepare error: {exc}")

        threading.Thread(target=_work, daemon=True).start()

    def _prepare_skip(self, panel, next_path, cue_sec, plan: TransitionPlan, my_epoch: int):
        """Prepare path for a no-stretch mix: load the track and stash the raw
        cue audio as the prepared buffer — no rubberband, no BPM animation. The
        incoming track plays at its natural tempo, so there's nothing to restore."""
        self._prep_animating = False
        self._prep_progress = 1.0
        panel.set_display_bpm(None)
        rel = f" ({plan.relation})" if plan.relation else ""
        panel.set_status(f"\\[PREPARING...] tempo matched{rel}, no stretch")
        self._status("Tempo within drift budget — preparing without stretch...")

        def _work():
            try:
                audio = self.engine.load_audio(next_path)
                cue_sample = int(cue_sec * SAMPLE_RATE)
                cue_audio = audio[cue_sample:]
                if self._prep_epoch != my_epoch:
                    return
                self._next_prepared = cue_audio
                self._next_plan = plan
                self._preparing = False
                self.call_from_thread(self._on_prepared)
            except Exception as exc:
                if self._prep_epoch == my_epoch:
                    self._preparing = False
                    self.call_from_thread(self._status, f"Prepare error: {exc}")

        threading.Thread(target=_work, daemon=True).start()

    def _on_prepared(self):
        self._prep_animating = False
        self._prep_progress = 1.0
        plan = self._next_plan
        panel = self.query_one("#next-track", NextTrackPanel)
        if plan is not None and plan.skip:
            rel = f" ({plan.relation})" if plan.relation else ""
            panel.set_status(f"\\[READY - press M] {self._next_bpm:.1f} BPM, no stretch{rel}")
        else:
            matched = plan.matched_bpm if plan is not None else self._now_bpm
            panel.set_status(f"\\[READY - press M] {self._next_bpm:.1f}→{matched:.1f} BPM")
        self._status("Mix prepared.")

    def action_mix_now(self):
        if self._next_prepared is None:
            self._status("Not prepared yet. Press P to prepare.")
            return
        # Nothing playing to mix out of (track finished or never started).
        if self.engine.state == State.IDLE:
            self._status("No track playing to mix from. Load one with Enter first.")
            return
        # Block mid-transition: the currently-playing buffer is either crossfading
        # or in the middle of its restoration ramp, so its current BPM doesn't match
        # the `start_rate` baked into the prepared buffer. Phase-specific messages
        # match the NowPlayingPanel banner.
        if self.engine.state == State.MIXING:
            self._status("Mixing the two tracks — cannot mix another track yet.")
            return
        if self._t_restore_start > 0.0:
            self._status("Restoring original tempo — cannot mix another track yet.")
            return
        panel = self.query_one("#next-track", NextTrackPanel)

        # Pick Track A's next downbeat as the schedule sample. We require both
        # tracks to have downbeats AND the cue to be snapped — otherwise either
        # side of the alignment is missing and we'd just be delaying the mix for
        # no musical reason.
        scheduled: Optional[int] = None
        if self._now_downbeats and self._next_downbeats and panel.cue_snapped:
            pos = self.engine.position
            safety = int(0.1 * SAMPLE_RATE)  # let the audio thread next-tick safely
            future = [d for d in self._now_downbeats if d > pos + safety]
            if future:
                scheduled = future[0]

        # BPM-display animation metadata — covers the rate ramp baked into the
        # precomputed buffer. On a SKIP mix the incoming track plays at its natural
        # tempo throughout (no rate ramp), so leave from/to at 0.0 — this keeps
        # _tick from arming a restoration ramp or overriding the now-playing BPM.
        skip = self._next_plan is not None and self._next_plan.skip
        if skip:
            restore_from = 0.0
            restore_to = 0.0
        else:
            restore_from = self._next_plan.matched_bpm if self._next_plan else self._now_bpm
            restore_to = self._next_bpm

        # Snapshot the incoming track's metadata BEFORE _commit_mix clears the slot.
        # Downbeats are rebased onto the prepared buffer (index 0 = the cue) so the NEXT
        # mix off this track schedules in engine coordinates, not the original frame.
        cue_sample = int(panel.cue * SAMPLE_RATE)
        swap = (
            self._next_path, self._next_bpm, self._next_key,
            self._buffer_downbeats(self._next_plan, cue_sample),
        )

        tail = " (no stretch)" if skip else ""
        base = "Mixing (no bar alignment)" if not panel.cue_snapped else "Mixing"
        status_deferred = (
            f"Waiting for downbeat at {_fmt_time(scheduled)}...{tail}"
            if scheduled is not None else ""
        )
        self._commit_mix(
            self._next_prepared, panel.fade, scheduled, swap,
            restore_from=restore_from,
            restore_to=restore_to,
            restore_seconds=max(0.5, panel.restore),
            status_immediate=f"{base}...{tail}",
            status_deferred=status_deferred,
        )

    def _commit_mix(
        self,
        buffer: np.ndarray,
        fade_seconds: float,
        scheduled: Optional[int],
        swap: tuple,
        *,
        restore_from: float,
        restore_to: float,
        restore_seconds: float,
        status_immediate: str,
        status_deferred: str,
    ) -> None:
        """Shared commit core for every crossfade (manual M-mix and the auto/emergency
        mix). Arms the restore-ramp metadata, hands the buffer to the engine, tears down
        the NEXT slot + cue preview, and performs the now-playing swap — immediately if
        the mix starts now, or deferred (held in _pending_now_swap) when it is bar-aligned
        and waiting for a downbeat. Callers snapshot `swap` from the next-track metadata
        BEFORE calling (this method clears that slot)."""
        # Restore-ramp display metadata for the rate ramp baked into the buffer; armed
        # by the MIXING→PLAYING transition in _tick(). 0/0 means no ramp (skip / as-is).
        self._restore_from_bpm = restore_from
        self._restore_to_bpm = restore_to
        self._restore_seconds = restore_seconds
        self._t_restore_start = 0.0

        self.engine.start_mix(buffer, fade_seconds, scheduled_start_sample=scheduled)

        # The NEXT slot is committed — clear it regardless of immediate/deferred, and
        # stop any cue preview of it (Q7 lifecycle).
        self._stop_cue()
        self.query_one("#next-track", NextTrackPanel).clear()
        self._next_path = None
        self._next_downbeats = []
        self._next_prepared = None
        self._next_plan = None

        if scheduled is not None:
            # Deferred (bar-aligned): hold the swap until the crossfade fires so the
            # NowPlaying panel keeps showing the outgoing track during the bar-wait.
            self._pending_now_swap = swap
            self._mix_scheduled = True
            self._status(status_deferred)
        else:
            # Immediate: engine is already MIXING, so swap the display now.
            self._apply_now_swap(swap)
            self._mix_scheduled = False
            self._status(status_immediate)

    def _apply_now_swap(self, swap: tuple) -> None:
        """Promote a queued next-track's metadata to now-playing and update the
        NowPlaying panel. Used by both the immediate and deferred mix paths."""
        path, bpm, key, downbeats = swap
        # Capture the outgoing track's name BEFORE overwriting _now_path, so the
        # panel can show "outgoing → incoming" for the crossfade. Cleared at
        # MIXING→PLAYING in _tick().
        outgoing = Path(self._now_path).name if self._now_path else None
        self._now_path = path
        self._now_bpm = bpm
        self._now_key = key
        self._now_downbeats = downbeats
        self.query_one("#now-playing", NowPlayingPanel).set_track(
            path, bpm, key, mix_from=outgoing
        )
        self._refresh_match_markers()
        self._pending_now_swap = None
        # A fresh track is now playing — re-arm the emergency one-shot so it gets its
        # own final-window mix (covers the deferred-swap path too).
        self._emergency_fired = False

    def _buffer_downbeats(self, plan: Optional[TransitionPlan], cue_sample: int) -> List[int]:
        """Rebase the incoming track's absolute downbeats onto its prepared buffer, whose
        index 0 is the cue — so a subsequent bar-aligned mix off this track schedules in
        the same coordinates the engine plays in (position 0 = the cue), not the track's
        original sample frame. A SKIP buffer is the raw cue audio, a clean constant shift
        (downbeats before the cue dropped). A STRETCH buffer time-warps the audio
        nonlinearly via the rubberband timemap, so absolute downbeats map to buffer
        positions by no constant — return [] and let the next mix fall back to immediate
        (non-aligned) start rather than schedule against warped data. Mirrors the backspin
        offset (which additionally prepends the SFX)."""
        if plan is not None and not plan.skip:
            return []
        return [d - cue_sample for d in self._next_downbeats if d >= cue_sample]

    # ------------------------------------------------------------------
    # Auto / emergency mix (A) — auto-mix the next track in the final window
    # ------------------------------------------------------------------

    def action_toggle_auto(self) -> None:
        """A binding: arm/disarm the auto/emergency mix. While armed, the playing
        track auto-mixes into the next one as it enters its final EMERGENCY_SECONDS."""
        self._auto_armed = not self._auto_armed
        self.query_one("#now-playing", NowPlayingPanel).set_auto(self._auto_armed)
        if self._auto_armed:
            self._status(
                f"Auto-mix armed - will mix the next track in the final "
                f"{int(EMERGENCY_SECONDS)} s."
            )
        else:
            self._status("Auto-mix disarmed.")

    def _next_song_in_folder(self, now_path: str) -> Optional[str]:
        """The next supported audio file after `now_path` within its own folder, in the
        same sorted order _load_songs_for uses. None when `now_path` is the last song in
        the folder (or isn't found). Non-recursive — one folder, mirroring the song list."""
        folder = os.path.dirname(now_path)
        try:
            entries = sorted(os.listdir(folder))
        except (OSError, PermissionError):
            return None
        songs = [
            os.path.join(folder, e)
            for e in entries
            if os.path.isfile(os.path.join(folder, e))
            and Path(e).suffix.lower() in SUPPORTED_EXTENSIONS
        ]
        try:
            idx = songs.index(now_path)
        except ValueError:
            return None
        return songs[idx + 1] if idx + 1 < len(songs) else None

    def _emergency_decision(self) -> Optional[dict]:
        """Pure classifier for the auto/emergency mix (no side effects, so probe_emergency
        can assert it directly). Returns None when the detector must stay quiet; otherwise
        a dict {case, ...}. Guards mirror action_mix_now plus the arm/one-shot state:
        fire only while armed, PLAYING (not paused), not already mid-/scheduled-transition
        or restoring, the one-shot unspent, and within the final EMERGENCY_SECONDS."""
        if not self._auto_armed or self._emergency_fired:
            return None
        if self.engine.state != State.PLAYING or self.engine.paused:
            return None
        if self._mix_scheduled or self._t_restore_start > 0.0:
            return None
        duration = self.engine.duration
        if duration <= 0:
            return None
        remaining = duration - self.engine.position
        if remaining > EMERGENCY_SECONDS * SAMPLE_RATE:
            return None
        # Classify by the NEXT-slot state.
        if self._next_path is None:
            source = self._next_song_in_folder(self._now_path) if self._now_path else None
            return {"case": 4, "source": source, "cue": 0.0}
        if self._next_prepared is not None:
            return {"case": 3}
        panel = self.query_one("#next-track", NextTrackPanel)
        if self._preparing:
            return {"case": 2, "source": self._next_path, "cue": panel.raw_cue}
        return {"case": 1, "source": self._next_path, "cue": panel.raw_cue}

    def _maybe_emergency_mix(self) -> None:
        """_tick hook: if the detector fires, dispatch the matching case. Claims the
        one-shot up front so the 100 ms ticks during the decode (and the can't-fire
        end-of-folder case) don't re-trigger."""
        d = self._emergency_decision()
        if d is None:
            return
        self._emergency_fired = True
        case = d["case"]

        if case == 4 and d["source"] is None:
            # Armed, but the playing song is the last in its folder — nothing to bring in.
            self._status("Auto-mix: last song in folder - nothing to mix.")
            return

        if case == 3:
            self._emergency_commit_prepared()
            return

        # Cases 1 / 2 / 4 mix the raw track as-is. Case 2 first drops the in-flight prep
        # (epoch bump orphans the worker, mirroring _mark_stale / action_load_next).
        if case == 2:
            self._prep_epoch += 1
            self._preparing = False
            self._prep_animating = False
            self._prep_progress = 0.0
            self._next_prepared = None
            self._next_plan = None
        self._emergency_commit_asis(d["source"], d["cue"], case)

    def _emergency_commit_prepared(self) -> None:
        """Case 3: crossfade the already-prepared buffer. Bar-aligns to Track A's next
        downbeat when one falls within the runway AND would still leave >= EMERGENCY_MIN_FADE
        of fade; else fires immediately. Fade = min(panel.fade, runway) so it stays inside
        the prepared buffer's constant-rate region (no beat drift) and never outlasts the
        audio left. Restore ramp arms exactly as in action_mix_now."""
        panel = self.query_one("#next-track", NextTrackPanel)
        pos = self.engine.position
        duration = self.engine.duration

        scheduled: Optional[int] = None
        if self._now_downbeats and self._next_downbeats and panel.cue_snapped:
            safety = int(0.1 * SAMPLE_RATE)
            future = [d for d in self._now_downbeats if d > pos + safety]
            if future and (duration - future[0]) >= EMERGENCY_MIN_FADE * SAMPLE_RATE:
                scheduled = future[0]

        runway_end = scheduled if scheduled is not None else pos
        fade = max(0.5, min(panel.fade, (duration - runway_end) / SAMPLE_RATE))

        skip = self._next_plan is not None and self._next_plan.skip
        if skip:
            restore_from = restore_to = 0.0
        else:
            restore_from = self._next_plan.matched_bpm if self._next_plan else self._now_bpm
            restore_to = self._next_bpm
        cue_sample = int(panel.cue * SAMPLE_RATE)
        swap = (
            self._next_path, self._next_bpm, self._next_key,
            self._buffer_downbeats(self._next_plan, cue_sample),
        )
        name = Path(swap[0]).name
        tail = " (no stretch)" if skip else ""
        deferred = (
            f"Auto-mix: waiting for downbeat at {_fmt_time(scheduled)}...{tail}"
            if scheduled is not None else ""
        )
        self._commit_mix(
            self._next_prepared, fade, scheduled, swap,
            restore_from=restore_from,
            restore_to=restore_to,
            restore_seconds=max(0.5, panel.restore),
            status_immediate=f"Auto-mix: beat-matched into {name}{tail}",
            status_deferred=deferred,
        )

    def _emergency_commit_asis(self, source: str, cue_sec: float, case: int) -> None:
        """Cases 1 / 2 / 4: decode the raw incoming track in a worker, then crossfade it
        as-is (natural tempo, no rubberband, no restore ramp). The fade is recomputed on
        the UI thread post-decode so it lands on the outgoing track's real end."""
        rec = self.library.get(source, empty_record())
        bpm, key = rec["bpm"], rec["key"]
        downbeats = list(rec["downbeats"])
        name = Path(source).name
        self._status(f"Auto-mix: bringing in {name}...")

        def _work():
            try:
                audio = self.engine.load_audio(source)
                cue_sample = int(cue_sec * SAMPLE_RATE)
                buf = audio[cue_sample:]
                # The buffer starts at cue_sample, so rebase the absolute library
                # downbeats onto the buffer (drop any before the cue) — else a later
                # bar-aligned mix off this track would land off the bar. Mirrors the
                # backspin offset (which also prepends the SFX); here there's no prefix.
                rel_downbeats = [d - cue_sample for d in downbeats if d >= cue_sample]
                self.call_from_thread(
                    self._start_emergency_asis, buf, source, bpm, key, rel_downbeats, case
                )
            except Exception as exc:
                self.call_from_thread(self._status, f"Auto-mix error: {exc}")

        threading.Thread(target=_work, daemon=True).start()

    def _start_emergency_asis(self, buf, source, bpm, key, downbeats, case) -> None:
        """UI-thread finisher for an as-is emergency mix: recompute the fade from the
        runway left NOW and commit. Bails if the track ended or was superseded (engine no
        longer PLAYING) while the decode was in flight."""
        if self.engine.state != State.PLAYING:
            return
        remaining = (self.engine.duration - self.engine.position) / SAMPLE_RATE
        fade = max(0.5, min(EMERGENCY_SECONDS, remaining))
        swap = (source, bpm, key, downbeats)
        prefix = {
            1: "Auto-mix: bringing in",
            2: "Auto-mix: dropped prep, bringing in",
            4: "Auto-mix: no queue - auto-selected",
        }.get(case, "Auto-mix: bringing in")
        name = Path(source).name
        self._commit_mix(
            buf, fade, None, swap,
            restore_from=0.0, restore_to=0.0, restore_seconds=0.0,
            status_immediate=f"{prefix} {name} (as-is)",
            status_deferred="",
        )

    # ------------------------------------------------------------------
    # Backspin transition (B)
    # ------------------------------------------------------------------

    def action_backspin(self):
        """Backspin/rewind transition: abruptly stop the outgoing track, play the
        backspin SFX one-shot, then start the queued next track at its natural tempo
        from its cue point. No crossfade, no rubberband — the SFX is simply prepended
        to the raw cue audio in one buffer. Requires a RAW next track: a prepared (or
        being-prepared) buffer is irrelevant to this mechanic, so B is rejected then."""
        if self.engine.state == State.IDLE:
            self._status("No track playing — load a track first.")
            return
        if not self._next_path:
            self._status("No next track queued (press N on a song first).")
            return
        if self._preparing:
            self._status("Next track is being prepared — cannot backspin.")
            return
        if self._next_prepared is not None:
            self._status(
                "Next track is prepared — backspin needs an un-prepared next track "
                "(press N to re-queue raw)."
            )
            return
        # Blocked mid-transition, mirroring action_mix_now: the engine is crossfading
        # or running a restoration ramp and slamming a backspin in would fight it.
        if self.engine.state == State.MIXING:
            self._status("Mixing the two tracks — cannot backspin yet.")
            return
        if self._t_restore_start > 0.0:
            self._status("Restoring original tempo — cannot backspin yet.")
            return
        if self._backspin_audio is None:
            self._status("Backspin sample still loading / failed to load.")
            return

        next_path = self._next_path
        next_bpm = self._next_bpm
        next_key = self._next_key
        next_downbeats = list(self._next_downbeats)
        # Backspin starts from the RAW cue (0:00 or exactly what the user typed) — not
        # the bar-snapped cue the crossfade uses.
        cue_sec = self.query_one("#next-track", NextTrackPanel).raw_cue
        # Defensive: the guards above already require no in-flight prep, but bump the
        # epoch so any worker that races in is orphaned (now-playing is changing).
        self._prep_epoch += 1
        self._status(f"Backspin → {Path(next_path).name}...")

        def _work():
            try:
                audio = self.engine.load_audio(next_path)
                cue_sample = int(cue_sec * SAMPLE_RATE)
                combined = np.concatenate(
                    [self._backspin_audio, audio[cue_sample:]], axis=0
                )
                self.call_from_thread(
                    self._apply_backspin,
                    combined, next_path, next_bpm, next_key, next_downbeats, cue_sample,
                )
            except Exception as exc:
                self.call_from_thread(self._status, f"Backspin error: {exc}")

        threading.Thread(target=_work, daemon=True).start()

    def _apply_backspin(
        self,
        combined: np.ndarray,
        next_path: str,
        next_bpm: float,
        next_key: str,
        next_downbeats: List[int],
        cue_sample: int,
    ) -> None:
        """UI-thread completion of the backspin: play the [SFX + cue audio] buffer and
        promote the next track to now-playing. The engine was PLAYING and stays PLAYING
        (play() just swaps _now_audio), so _tick's MIXING→PLAYING / →IDLE detectors do
        not fire — no spurious restoration ramp or _handle_track_finished."""
        self.engine.play(combined)

        self._now_path = next_path
        self._now_bpm = next_bpm
        self._now_key = next_key
        # The SFX of length L is prepended and the track is cut at cue_sample, so a
        # downbeat at absolute index d lands at engine position L + (d - cue_sample).
        # Offsetting keeps a subsequent bar-aligned mix lined up.
        offset = len(self._backspin_audio) - cue_sample
        self._now_downbeats = [d + offset for d in next_downbeats if d >= cue_sample]

        # Plays at its natural tempo — no rate ramp, nothing to restore (like a SKIP).
        self._restore_from_bpm = 0.0
        self._restore_to_bpm = 0.0
        self._t_restore_start = 0.0
        self._mix_scheduled = False
        self._pending_now_swap = None
        self._emergency_fired = False   # fresh now-playing track — re-arm the one-shot

        # The NEXT slot is consumed — stop any cue preview of it (Q7 lifecycle).
        self._stop_cue()
        self.query_one("#next-track", NextTrackPanel).clear()
        self._next_path = None
        self._next_bpm = 0.0
        self._next_key = ""
        self._next_downbeats = []
        self._next_prepared = None
        self._next_plan = None

        # Abrupt cut, not a crossfade — no mix_from on the panel.
        self.query_one("#now-playing", NowPlayingPanel).set_track(next_path, next_bpm, next_key)
        self._refresh_match_markers()
        self._status(f"Backspin → {Path(next_path).name}")

    # ------------------------------------------------------------------
    # Pre-listen cue / PFL (L, [ , ])
    # ------------------------------------------------------------------

    def _stop_cue(self) -> None:
        """Stop any active cue preview and orphan any in-flight cue decode. Safe
        to call when no cue device is configured. Bumping the epoch makes a
        decode worker discard its buffer instead of auditioning a stale track."""
        self._cue_epoch += 1
        self._cue_loading = False
        if self._cue is not None:
            self._cue.stop()

    def action_cue_toggle(self) -> None:
        """L: toggle pre-listening the queued NEXT track in the cue headphones.
        Independent of the master state machine — allowed whatever the engine is
        doing (the whole point is cueing the next track over the current mix).
        Stopped → decode + play from the RAW cue (the honest drop point, matching
        backspin); playing-or-loading → stop. A double-tap during the decode hits
        the loading branch and stops, bumping the epoch so the worker self-discards."""
        if self._cue is None:
            self._status("No cue device — relaunch with --headphones-device <name> to enable PFL.")
            return
        if self._cue.is_dead:
            self._status("Cue device was lost. Restart with the device connected.")
            return
        if self._cue.is_playing or self._cue_loading:
            self._stop_cue()
            self._status("Cue stopped.")
            return
        if not self._next_path:
            self._status("No next track queued to cue (press N on a song first).")
            return

        self._cue_epoch += 1
        my_epoch = self._cue_epoch
        self._cue_loading = True
        next_path = self._next_path
        cue_sec = self.query_one("#next-track", NextTrackPanel).raw_cue
        self._status(f"Cueing {Path(next_path).name}...")

        def _work():
            try:
                audio = self.engine.load_audio(next_path)
                cue_sample = int(cue_sec * SAMPLE_RATE)
                buf = audio[cue_sample:]
                if self._cue_epoch != my_epoch:
                    return  # superseded by re-press / N / M / B — drop the buffer
                self.call_from_thread(self._start_cue_playback, buf, next_path, my_epoch)
            except Exception as exc:
                if self._cue_epoch == my_epoch:
                    self._cue_loading = False
                    self.call_from_thread(self._status, f"Cue error: {exc}")

        threading.Thread(target=_work, daemon=True).start()

    def _start_cue_playback(self, buf: np.ndarray, next_path: str, my_epoch: int) -> None:
        """UI-thread hand-off for a completed cue decode. A final epoch check
        closes the race where the NEXT slot changed between the worker's check
        and this call."""
        if self._cue is None or self._cue_epoch != my_epoch:
            return
        self._cue_loading = False
        self._cue.play(buf)
        self._status(f"Cueing {Path(next_path).name}")

    def _handle_track_finished(self) -> None:
        """Reset app state when the now-playing track ends naturally (engine went
        IDLE without an explicit Stop). Clears now-playing, invalidates any in-flight
        or completed prep (its start_rate was rendered against the track that just
        ended), and drops transition/animation state. A queued NEXT track is kept
        but marked unprepared so the user keeps their selection."""
        self._now_path = None
        self._now_bpm = 0.0
        self._now_key = ""
        self._now_downbeats = []
        self._t_restore_start = 0.0
        self._restore_from_bpm = 0.0
        self._restore_to_bpm = 0.0
        self._mix_scheduled = False
        self._pending_now_swap = None
        self._emergency_fired = False
        # Invalidate prep — its buffer (if any) was matched to the finished track.
        self._prep_epoch += 1
        self._preparing = False
        self._prep_animating = False
        self._prep_progress = 0.0
        self._next_prepared = None
        self._next_plan = None
        # Cancel any in-progress C/F/R text entry: the playback context just
        # changed out from under it, and leaving _input_mode active would let the
        # next keystroke re-stomp the "Track finished" status with a stale prompt.
        self._input_mode = ""
        self._input_buf = ""

        self.query_one("#now-playing", NowPlayingPanel).clear()
        self._refresh_match_markers()
        if self._next_path is not None:
            panel = self.query_one("#next-track", NextTrackPanel)
            panel.set_display_bpm(None)
            panel.set_status("\\[not prepared]")
            self._status("Track finished. Press Enter to load a song (next track still queued).")
        else:
            self._status("Track finished. Select a song and press Enter.")

    def action_set_cue(self):
        if not self._next_path:
            self._status("No next track selected")
            return
        self._start_input("cue", "Enter cue point in seconds: ")

    def action_set_fade(self):
        if not self._next_path:
            self._status("No next track selected")
            return
        self._start_input("fade", "Enter fade duration in seconds: ")

    def action_set_restore(self):
        if not self._next_path:
            self._status("No next track selected")
            return
        self._start_input("restore", "Enter restore duration in seconds: ")

    # ------------------------------------------------------------------
    # Inline text input (cue / fade)
    # ------------------------------------------------------------------

    def _start_input(self, mode: str, prompt: str):
        self._input_mode = mode
        self._input_buf = ""
        self._status(f"{prompt}_")

    def on_key(self, event: events.Key):
        if not self._input_mode:
            # Cue seek: match the RESOLVED character ("[" / "]"), NOT event.key,
            # so AltGr-composed brackets on non-US layouts (Italian: AltGr+è / +)
            # work regardless of how the terminal names the physical key. Only
            # while a cue is actively playing; otherwise fall through to nav.
            if (
                event.character in ("[", "]")
                and self._cue is not None
                and self._cue.is_playing
            ):
                self._cue.seek(
                    -CUE_SEEK_SECONDS if event.character == "[" else CUE_SEEK_SECONDS
                )
                event.prevent_default()
                event.stop()
                return
            # Master volume: , (down) / . (up). Matched on the resolved character
            # (AltGr/Shift-safe; , and . are adjacent + unshifted on every layout);
            # allowed in any engine state. The master can boost above 100%.
            if event.character in (",", "."):
                step = VOLUME_STEP if event.character == "." else -VOLUME_STEP
                self.engine.set_volume(self.engine.volume + step)
                pct = round(self.engine.volume * 100)
                tail = " (boost)" if self.engine.volume > 1.0 else ""
                self._status(f"Master volume {pct}%{tail}")
                event.prevent_default()
                event.stop()
                return
            # Headphone-cue volume: 9 / 0 (only when a cue device is configured).
            if event.character in ("9", "0") and self._cue is not None:
                step = VOLUME_STEP if event.character == "0" else -VOLUME_STEP
                self._cue.set_volume(self._cue.volume + step)
                self._status(f"Cue volume {round(self._cue.volume * 100)}%")
                event.prevent_default()
                event.stop()
                return
            # FX effect select (1/2/3). Matched on the resolved character so digits work
            # on non-US layouts (Italian needs character matching, like the ,/./9/0
            # volume keys) — that's why these are NOT BINDINGS. The gate toggle itself is
            # the "g" Binding (action_fx_gate); "g" is a plain letter, so a binding is
            # layout-safe and shows in the Footer. Intensity is the mouse wheel (_fx_wheel).
            if event.character in ("1", "2", "3"):
                self._select_fx({"1": "hpf", "2": "lpf", "3": "trans"}[event.character])
                event.prevent_default()
                event.stop()
                return
            if event.key == "left" and isinstance(self.focused, DataTable):
                # Exit the song list back to the tree, collapsing the folder we
                # were browsing so its arrow returns to pointing right (shared with
                # the second-activation toggle in on_tree_node_selected).
                self._exit_song_browsing()
                event.prevent_default()
                event.stop()
            return

        if event.key in ("escape",):
            self._input_mode = ""
            self._input_buf = ""
            self._status("")
            event.prevent_default()
            event.stop()
            return

        if event.key == "enter":
            try:
                value = float(self._input_buf)
                panel = self.query_one("#next-track", NextTrackPanel)
                if self._input_mode == "cue":
                    cue_before = panel.cue
                    panel.set_cue(value)
                    cue_after = panel.cue
                    self._status(self._cue_status_message(value, cue_before, cue_after, panel))
                elif self._input_mode == "fade":
                    panel.set_fade(value)
                    self._status(f"Fade duration set to {value:.1f}s")
                elif self._input_mode == "restore":
                    panel.set_restore(value)
                    self._status(f"Restore duration set to {value:.1f}s")
                self._mark_stale()
            except ValueError:
                self._status("Invalid number. Cancelled.")
            self._input_mode = ""
            self._input_buf = ""
            event.prevent_default()
            event.stop()
            return

        if event.key == "backspace":
            self._input_buf = self._input_buf[:-1]
        elif event.character and (event.character.isdigit() or event.character == "."):
            self._input_buf += event.character

        prompts = {"cue": "cue point", "fade": "fade duration", "restore": "restore duration"}
        prompt = prompts.get(self._input_mode, self._input_mode)
        self._status(f"Enter {prompt} in seconds: {self._input_buf}_")
        event.prevent_default()
        event.stop()

    # ------------------------------------------------------------------
    # Stale-prep handling (C/F/R changed after Prepare)
    # ------------------------------------------------------------------

    def _mark_stale(self) -> None:
        if self._next_prepared is not None or self._preparing:
            # Bump epoch so an in-flight prep with the old cue/fade/restore won't
            # silently install its (now stale) buffer when it finishes.
            self._prep_epoch += 1
            self._next_prepared = None
            self._next_plan = None
            self._preparing = False
            self._prep_animating = False
            self._prep_progress = 0.0
            panel = self.query_one("#next-track", NextTrackPanel)
            panel.set_display_bpm(None)
            panel.set_status("\\[STALE - press P]")

    # ------------------------------------------------------------------
    # Periodic tick
    # ------------------------------------------------------------------

    def _tick(self):
        now_t = time.time()
        dt = now_t - self._last_tick_t
        self._last_tick_t = now_t

        current_state = self.engine.state

        # Freeze the wall-clock-driven restoration ramp while audio is paused, so
        # the displayed BPM doesn't run ahead of the (frozen) playback. Shifting
        # the start forward by dt keeps (now - start) constant across the pause.
        if self._t_restore_start > 0.0 and self.engine.paused:
            self._t_restore_start += dt

        # A scheduled mix just fired — apply the deferred now-playing swap and
        # replace the "Waiting for downbeat..." status.
        if self._mix_scheduled and current_state == State.MIXING:
            self._mix_scheduled = False
            if self._pending_now_swap is not None:
                self._apply_now_swap(self._pending_now_swap)
            self._status("Mixing...")

        # The now-playing track finished on its own (or a mixed-in track ran out) —
        # the engine flipped to IDLE without an explicit Stop. Reset to a clean
        # idle state so Enter can load a fresh track and no stale prep/buffer lingers.
        if (
            self._prev_engine_state in (State.PLAYING, State.MIXING)
            and current_state == State.IDLE
            and self._now_path is not None
        ):
            self._handle_track_finished()

        # The crossfade just ended — drop the "outgoing → incoming" dual name (only
        # the incoming track plays now) and arm the BPM-display animation for the
        # rate ramp that's about to play out inside the precomputed buffer.
        if self._prev_engine_state == State.MIXING and current_state == State.PLAYING:
            self.query_one("#now-playing", NowPlayingPanel).clear_mix_from()
            if self._restore_to_bpm > 0 and abs(self._restore_from_bpm - self._restore_to_bpm) > 0.01:
                self._t_restore_start = time.time()
        self._prev_engine_state = current_state

        # Auto/emergency mix: when armed, fire a crossfade into the next track as the
        # playing one enters its final window. The decision re-reads the live engine
        # state itself, so it's safe to call unconditionally here.
        self._maybe_emergency_mix()

        # Prep-BPM animation in NextTrackPanel (driven by rubberband's own progress)
        if self._prep_animating and self._next_path:
            t = _smootherstep(self._prep_progress)
            anim_bpm = self._prep_from_bpm + (self._prep_to_bpm - self._prep_from_bpm) * t
            self.query_one("#next-track", NextTrackPanel).set_display_bpm(anim_bpm)

        # NowPlayingPanel BPM display + phase banner
        if self._now_path and current_state != State.IDLE:
            panel = self.query_one("#now-playing", NowPlayingPanel)
            if current_state == State.MIXING and self._restore_from_bpm > 0:
                current_bpm: Optional[float] = self._restore_from_bpm
            elif self._t_restore_start > 0.0:
                t = (time.time() - self._t_restore_start) / max(self._restore_seconds, 0.1)
                if t >= 1.0:
                    current_bpm = self._restore_to_bpm
                    self._t_restore_start = 0.0
                else:
                    current_bpm = (
                        self._restore_from_bpm
                        + (self._restore_to_bpm - self._restore_from_bpm) * _smootherstep(t)
                    )
            else:
                current_bpm = None

            # Keep the Trans gate locked to the audio's live tempo (follows the
            # restoration ramp when one is playing out, else the track's BPM). Only while
            # the gate is engaged, so an idle gate costs no per-tick lock acquire.
            if self._fx_enabled:
                self.engine.set_fx_tempo(current_bpm if current_bpm is not None else self._now_bpm)

            # Phase banner reflects whether action_mix_now would be accepted right
            # now; the wording matches the status-line block messages so the user
            # has one consistent story.
            if current_state == State.MIXING:
                panel.set_phase("MIXING the two tracks - cannot mix another track")
            elif self._mix_scheduled:
                panel.set_phase("WAITING for downbeat - cannot mix another track")
            elif self._t_restore_start > 0.0:
                panel.set_phase("RESTORING original tempo - cannot mix another track")
            else:
                panel.set_phase("Ready to mix another song")
            # During the crossfade, hand the panel the incoming track's progress
            # too so it can show both bars (outgoing → incoming) side by side.
            if current_state == State.MIXING:
                mix_position: Optional[int] = self.engine.mix_position
                mix_duration: Optional[int] = self.engine.mix_duration
            else:
                mix_position = mix_duration = None
            panel.refresh_progress(
                self.engine.position,
                self.engine.duration,
                current_bpm,
                mix_position,
                mix_duration,
                master_vol=self.engine.volume,
            )

        # Cue/PFL: surface a lost device once, and feed the live cue line on the
        # NextTrackPanel. The cue is independent of the master state machine, so
        # this runs regardless of engine state.
        if self._cue is not None:
            if self._cue.is_dead and not self._cue_dead_reported:
                self._cue_dead_reported = True
                self._cue_loading = False
                self._status("Cue device lost — cueing disabled (reconnect and restart).")
            if self._next_path:
                self.query_one("#next-track", NextTrackPanel).set_cue_state(
                    self._cue.is_playing,
                    self._cue.position_seconds,
                    self._cue.duration_seconds,
                    self._cue.volume,
                )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _cue_status_message(
        self, typed: float, cue_before: float, cue_after: float, panel: "NextTrackPanel"
    ) -> str:
        """Status line wording for the Cue input. Surfaces unexpected snap outcomes
        (no-op, or a jump > 1 bar) so the user can tell the snap happened and isn't
        confused when typing a value appears to do nothing."""
        if not panel.cue_snapped:
            return f"Cue point set to {typed:.1f}s (no downbeats — no bar lock)"
        bar_seconds = (60.0 / self._next_bpm) * 4 if self._next_bpm > 0 else 2.0
        drift = typed - cue_after
        if abs(cue_after - cue_before) < 0.001:
            return (
                f"Cue unchanged: nearest downbeat to {typed:.1f}s is still {cue_after:.2f}s"
            )
        if abs(drift) > bar_seconds:
            return (
                f"Cue snapped: {typed:.1f}s -> {cue_after:.2f}s "
                f"(no downbeat closer than {abs(drift):.1f}s — gap in beat detection)"
            )
        return f"Cue snapped to downbeat at {cue_after:.2f}s"

    def _selected_song(self) -> Optional[str]:
        table = self.query_one("#song-list", DataTable)
        if not self._songs_in_view:
            return None
        row = table.cursor_row
        if row < 0 or row >= len(self._songs_in_view):
            return None
        return self._songs_in_view[row]

    def _do_load_now(self, path: str):
        rec = self.library.get(path, empty_record())
        bpm, key = rec["bpm"], rec["key"]
        downbeats = list(rec["downbeats"])
        # Clear any in-flight transition state so the new track's BPM display isn't
        # hijacked by a prior mix's restoration ramp. Without this, the previous
        # transition's `_t_restore_start` and from/to BPMs keep driving the
        # NowPlayingPanel animation against the freshly loaded track. (Also
        # suppresses the spurious MIXING→PLAYING re-arming when this load
        # interrupts an active crossfade.)
        self._t_restore_start = 0.0
        self._restore_from_bpm = 0.0
        self._restore_to_bpm = 0.0
        self._mix_scheduled = False
        self._emergency_fired = False   # fresh now-playing track — re-arm the one-shot
        # Orphan any in-flight prep: it was rendered with start_rate computed
        # against the OLD now-playing BPM, so its buffer is wrong for the
        # newly-loaded track. The bump makes the prep worker silently discard
        # its buffer + progress writes on completion.
        self._prep_epoch += 1
        self._preparing = False
        self._prep_animating = False
        self._prep_progress = 0.0
        self._status(f"Loading: {Path(path).name}...")

        def _work():
            try:
                audio = self.engine.load_audio(path)
                self._now_path = path
                self._now_bpm = bpm
                self._now_key = key
                self._now_downbeats = downbeats
                self.engine.play(audio)
                self.call_from_thread(
                    lambda: (
                        self.query_one("#now-playing", NowPlayingPanel).set_track(path, bpm, key),
                        self._refresh_match_markers(),
                        self._status(f"Now playing: {Path(path).name}"),
                    )
                )
            except Exception as exc:
                self.call_from_thread(self._status, f"Load error: {exc}")

        threading.Thread(target=_work, daemon=True).start()

    def _status(self, msg: str):
        self.query_one("#status-bar", Static).update(msg)

    def on_unmount(self):
        self.engine.close()
        if self._cue is not None:
            self._cue.close()
