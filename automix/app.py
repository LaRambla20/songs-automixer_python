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
from textual.widgets import DataTable, Footer, Header, Label, Static, Tree
from textual.widgets.tree import TreeNode

from .audio_engine import AudioEngine, State, SAMPLE_RATE
from .analyzer import SUPPORTED_EXTENSIONS, empty_record
from .stretcher import make_transition_buffer
from .transition import plan_transition, tempo_compatible, TransitionPlan


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _smootherstep(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return x * x * x * (x * (x * 6 - 15) + 10)


def _fmt_time(samples: int) -> str:
    secs = max(0, samples) // SAMPLE_RATE
    return f"{secs // 60}:{secs % 60:02d}"


# ---------------------------------------------------------------------------
# Sub-widgets
# ---------------------------------------------------------------------------

class NowPlayingPanel(Static):
    def __init__(self, **kwargs):
        super().__init__("NOW PLAYING: \\[no track loaded]", **kwargs)
        self._path: Optional[str] = None
        self._bpm: float = 0.0
        self._key: str = ""
        # Set by _tick to surface the current transition phase (MIXING vs
        # tempo-restoration vs idle). Drives the third panel line.
        self._phase: str = ""

    def set_track(self, path: str, bpm: float, key: str):
        self._path = path
        self._bpm = bpm
        self._key = key
        self._phase = ""
        self.refresh_progress(0, 0)

    def set_phase(self, phase: str) -> None:
        self._phase = phase

    def refresh_progress(self, position: int, duration: int, current_bpm: Optional[float] = None):
        if self._path is None:
            self.update("NOW PLAYING: \\[no track loaded]")
            return
        name = _escape(Path(self._path).name)
        pct = position / duration if duration > 0 else 0.0
        bar_len = 32
        filled = int(pct * bar_len)
        bar = "#" * filled + "-" * (bar_len - filled)
        bpm = current_bpm if current_bpm is not None else self._bpm
        lines = [
            f"NOW PLAYING: {name}  |  {bpm:.1f} BPM  {self._key}",
            f"  {_fmt_time(position)} / {_fmt_time(duration)}  [{bar}]",
        ]
        if self._phase:
            lines.append(f"  {self._phase}")
        self.update("\n".join(lines))

    def clear(self):
        self._path = None
        self._phase = ""
        self.update("NOW PLAYING: \\[no track loaded]")


class NextTrackPanel(Static):
    def __init__(self, **kwargs):
        super().__init__("NEXT TRACK: \\[none]", **kwargs)
        self._path: Optional[str] = None
        self._bpm: float = 0.0
        self._key: str = ""
        self._cue: float = 0.0
        self._fade: float = 16.0
        self._restore: float = 30.0
        self._status: str = ""
        self._display_bpm: Optional[float] = None
        # Per-track downbeat sample indices (at engine SAMPLE_RATE). When non-empty,
        # set_cue auto-snaps to the nearest one — this is what gives the mix its
        # bar alignment.
        self._downbeats: List[int] = []
        self._cue_snapped: bool = False

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
            self.set_cue(self._cue)

    def set_cue(self, seconds: float):
        target = max(0.0, seconds)
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
        cue_str = _fmt_time(int(self._cue * SAMPLE_RATE))
        cue_marker = " \\[bar]" if self._cue_snapped else ""
        bpm_show = self._display_bpm if self._display_bpm is not None else self._bpm
        self.update(
            f"NEXT TRACK: {name}  |  {bpm_show:.1f} BPM  {self._key}\n"
            f"  Cue: {cue_str}{cue_marker}  Fade: {self._fade:.0f}s  Restore: {self._restore:.0f}s  {self._status}\n"
            f"  \\[C] Cue  \\[F] Fade  \\[R] Restore  \\[P] Prepare  \\[M] Mix"
        )

    def clear(self):
        self._path = None
        self._display_bpm = None
        self._downbeats = []
        self._cue_snapped = False
        self.update("NEXT TRACK: \\[none]")


# ---------------------------------------------------------------------------
# Folder tree
# ---------------------------------------------------------------------------

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
                node.expand()
                # Root just reveals its subfolders and keeps focus on the tree
                # (root display -> subfolder display), moving the highlight down
                # onto the first subfolder. A subfolder additionally loads its
                # songs and hands focus to the song panel.
                if node is self.root:
                    if node.children:
                        self.cursor_line = node.line + 1
                else:
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
                parent.collapse()
            elif node.allow_expand and node.is_expanded:
                # Fallback: an expanded node with no displayable parent (the root in
                # subfolder display) — collapse it in place.
                event.prevent_default()
                event.stop()
                node.collapse()
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


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class AutoMixApp(App):
    CSS = """
    Screen {
        background: #0d0d0d;
        color: #00ff41;
    }
    Header {
        background: #001a00;
        color: #00ff41;
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
    Tree > .tree--cursor {
        background: #003300;
        color: #00ff41;
    }
    DataTable {
        background: #0d0d0d;
        color: #00ff41;
        scrollbar-color: #005500;
    }
    DataTable > .datatable--cursor {
        background: #003300;
        color: #00ff41;
    }
    DataTable > .datatable--header {
        background: #001a00;
        color: #00cc33;
        text-style: bold;
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
        height: 5;
        padding: 0 1;
    }
    #status-bar {
        height: 1;
        background: #001a00;
        color: #007700;
        padding: 0 1;
    }
    Footer {
        background: #001a00;
        color: #005500;
    }
    """

    BINDINGS = [
        Binding("space", "toggle_pause", "Play/Pause", show=True),
        Binding("s", "stop", "Stop", show=True),
        Binding("n", "load_next", "-> Next", show=True),
        Binding("p", "prepare_mix", "Prepare", show=True),
        Binding("m", "mix_now", "Mix Now", show=True),
        Binding("c", "set_cue", "Set Cue", show=True),
        Binding("f", "set_fade", "Set Fade", show=True),
        Binding("r", "set_restore", "Restore", show=True),
        Binding("q", "quit", "Quit", show=True),
    ]

    def __init__(self, root_folder: str, library: Dict[str, Dict]):
        super().__init__()
        self.root_folder = root_folder
        self.library = library

        self.engine = AudioEngine()

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
        yield Header(show_clock=True)
        with Horizontal(id="browser"):
            with Vertical(id="folder-panel"):
                yield Label("FOLDERS", id="folder-label")
                yield FolderTree(Path(self.root_folder).name, id="folder-tree")
            with Vertical(id="song-panel"):
                yield Label("SONGS  [Enter] Now Playing  [N] Next Track", id="song-label")
                yield DataTable(id="song-list")
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

    def on_tree_node_selected(self, event: Tree.NodeSelected):
        # Enter / mouse-click. The FolderTree has auto_expand disabled, so we drive
        # expansion explicitly here. Root just reveals its subfolders and keeps
        # focus on the tree (root display -> subfolder display); a subfolder expands,
        # loads its songs, and hands focus to the song panel — same three gestures
        # (Enter / click / right-arrow) that the GoToSongs contract covers.
        node = event.node
        if not (node.data and isinstance(node.data, str) and os.path.isdir(node.data)):
            return
        tree = self.query_one("#folder-tree", FolderTree)
        node.expand()
        if node is tree.root:
            # root display -> subfolder display: move the highlight onto the
            # first subfolder so the user is positioned to drill in.
            if node.children:
                tree.cursor_line = node.line + 1
            return
        self._load_songs_for(node.data)
        self.query_one("#song-list", DataTable).focus()

    def on_folder_tree_go_to_songs(self, event: FolderTree.GoToSongs) -> None:
        self._load_songs_for(event.folder)
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
        self.query_one("#now-playing", NowPlayingPanel).clear()
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
        if self._next_plan is not None and self._next_plan.skip:
            self._restore_from_bpm = 0.0
            self._restore_to_bpm = 0.0
        else:
            self._restore_from_bpm = self._next_plan.matched_bpm if self._next_plan else self._now_bpm
            self._restore_to_bpm = self._next_bpm
        self._restore_seconds = max(0.5, panel.restore)
        self._t_restore_start = 0.0   # armed by the MIXING→PLAYING transition in _tick()

        skip = self._next_plan is not None and self._next_plan.skip
        self.engine.start_mix(self._next_prepared, panel.fade, scheduled_start_sample=scheduled)

        # Snapshot the incoming track's metadata for the now-playing swap.
        swap = (self._next_path, self._next_bpm, self._next_key, list(self._next_downbeats))

        # The NEXT slot is committed — clear it regardless of immediate/deferred.
        self.query_one("#next-track", NextTrackPanel).clear()
        self._next_path = None
        self._next_downbeats = []
        self._next_prepared = None
        self._next_plan = None

        tail = " (no stretch)" if skip else ""
        if scheduled is not None:
            # Deferred (bar-aligned): hold the swap until the crossfade fires so the
            # NowPlaying panel keeps showing the outgoing track during the bar-wait.
            self._pending_now_swap = swap
            self._mix_scheduled = True
            self._status(f"Waiting for downbeat at {_fmt_time(scheduled)}...{tail}")
        else:
            # Immediate: engine is already MIXING, so swap the display now.
            self._apply_now_swap(swap)
            self._mix_scheduled = False
            base = "Mixing (no bar alignment)" if not panel.cue_snapped else "Mixing"
            self._status(f"{base}...{tail}")

    def _apply_now_swap(self, swap: tuple) -> None:
        """Promote a queued next-track's metadata to now-playing and update the
        NowPlaying panel. Used by both the immediate and deferred mix paths."""
        path, bpm, key, downbeats = swap
        self._now_path = path
        self._now_bpm = bpm
        self._now_key = key
        self._now_downbeats = downbeats
        self.query_one("#now-playing", NowPlayingPanel).set_track(path, bpm, key)
        self._refresh_match_markers()
        self._pending_now_swap = None

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
            if event.key == "left" and isinstance(self.focused, DataTable):
                table = self.query_one("#song-list", DataTable)
                table.clear()
                self._songs_in_view = []
                tree = self.query_one("#folder-tree", FolderTree)
                # Collapse the folder we were browsing so its arrow returns to
                # pointing right — exiting the song list should undo the expansion
                # that entering it produced. Skip the root (collapsing it would
                # hide the whole tree).
                node = tree.cursor_node
                if (
                    node is not None
                    and node is not tree.root
                    and node.allow_expand
                    and node.is_expanded
                ):
                    node.collapse()
                tree.focus()
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

        # The crossfade just ended — arm the BPM-display animation for the rate ramp
        # that's about to play out inside the precomputed buffer.
        if self._prev_engine_state == State.MIXING and current_state == State.PLAYING:
            if self._restore_to_bpm > 0 and abs(self._restore_from_bpm - self._restore_to_bpm) > 0.01:
                self._t_restore_start = time.time()
        self._prev_engine_state = current_state

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

            # Phase banner reflects whether action_mix_now would be accepted right
            # now; the wording matches the status-line block messages so the user
            # has one consistent story.
            if current_state == State.MIXING:
                panel.set_phase("MIXING the two tracks - cannot mix another track")
            elif self._t_restore_start > 0.0:
                panel.set_phase("RESTORING original tempo - cannot mix another track")
            else:
                panel.set_phase("Ready to mix another song")
            panel.refresh_progress(self.engine.position, self.engine.duration, current_bpm)

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
