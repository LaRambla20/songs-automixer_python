import os
import time
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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
from .analyzer import SUPPORTED_EXTENSIONS
from .stretcher import make_transition_buffer


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

    def set_track(self, path: str, bpm: float, key: str):
        self._path = path
        self._bpm = bpm
        self._key = key
        self.refresh_progress(0, 0)

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
        self.update(
            f"NOW PLAYING: {name}  |  {bpm:.1f} BPM  {self._key}\n"
            f"  {_fmt_time(position)} / {_fmt_time(duration)}  [{bar}]"
        )

    def clear(self):
        self._path = None
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

    def set_track(self, path: str, bpm: float, key: str):
        self._path = path
        self._bpm = bpm
        self._key = key
        self._display_bpm = None
        self._status = "\\[not prepared]"
        self._render()

    def set_cue(self, seconds: float):
        self._cue = max(0.0, seconds)
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
        bpm_show = self._display_bpm if self._display_bpm is not None else self._bpm
        self.update(
            f"NEXT TRACK: {name}  |  {bpm_show:.1f} BPM  {self._key}\n"
            f"  Cue: {cue_str}  Fade: {self._fade:.0f}s  Restore: {self._restore:.0f}s  {self._status}\n"
            f"  \\[C] Cue  \\[F] Fade  \\[R] Restore  \\[P] Prepare  \\[M] Mix"
        )

    def clear(self):
        self._path = None
        self._display_bpm = None
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
                self.post_message(self.GoToSongs(node.data))


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
        height: 4;
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

    def __init__(self, root_folder: str, library: Dict[str, Tuple[float, str]]):
        super().__init__()
        self.root_folder = root_folder
        self.library = library

        self.engine = AudioEngine()

        self._now_path: Optional[str] = None
        self._now_bpm: float = 0.0
        self._now_key: str = ""

        self._next_path: Optional[str] = None
        self._next_bpm: float = 0.0
        self._next_key: str = ""
        self._next_prepared: Optional[np.ndarray] = None
        self._preparing: bool = False

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
        self._load_songs_for(self.root_folder)
        self.set_interval(0.1, self._tick)

    def _setup_song_table(self):
        t = self.query_one("#song-list", DataTable)
        t.add_columns("Filename", "BPM", "Key")
        t.cursor_type = "row"

    def _build_tree(self):
        tree = self.query_one("#folder-tree", Tree)
        tree.root.data = self.root_folder
        tree.root.expand()
        self._populate_node(tree.root, self.root_folder)

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
        if event.node.data and isinstance(event.node.data, str) and os.path.isdir(event.node.data):
            self._load_songs_for(event.node.data)

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
                bpm, key = self.library.get(full, (0.0, "?"))
                bpm_str = f"{bpm:.1f}" if bpm > 0 else "---"
                table.add_row(entry, bpm_str, key, key=full)
                self._songs_in_view.append(full)

    # ------------------------------------------------------------------
    # Song table events
    # ------------------------------------------------------------------

    def on_data_table_row_selected(self, event: DataTable.RowSelected):
        """Enter on a song row -> load as Now Playing."""
        path = self._selected_song()
        if path:
            self._do_load_now(path)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_toggle_pause(self):
        self.engine.pause()

    def action_stop(self):
        self.engine.stop()
        self._now_path = None
        self._t_restore_start = 0.0
        self._prep_animating = False
        self._prep_progress = 0.0
        self.query_one("#now-playing", NowPlayingPanel).clear()
        self._status("")

    def action_seek_back(self):
        self.engine.seek(-10 * SAMPLE_RATE)

    def action_seek_fwd(self):
        self.engine.seek(10 * SAMPLE_RATE)

    def action_load_next(self):
        path = self._selected_song()
        if not path:
            return
        bpm, key = self.library.get(path, (0.0, "?"))
        self._next_path = path
        self._next_bpm = bpm
        self._next_key = key
        self._next_prepared = None
        panel = self.query_one("#next-track", NextTrackPanel)
        panel.set_track(path, bpm, key)
        self._status(f"Queued as next: {Path(path).name}")

    def action_prepare_mix(self):
        if not self._next_path:
            self._status("No next track selected (press N on a song first)")
            return
        if self._preparing:
            self._status("Already preparing...")
            return
        self._preparing = True
        panel = self.query_one("#next-track", NextTrackPanel)
        now_bpm = self._now_bpm
        next_bpm = self._next_bpm
        next_path = self._next_path
        cue_sec = panel.cue
        fade_sec = panel.fade
        restore_sec = panel.restore
        self._prep_animating = True
        self._prep_progress = 0.0
        self._prep_from_bpm = next_bpm
        self._prep_to_bpm = now_bpm
        panel.set_status(f"\\[PREPARING...] {next_bpm:.1f}→{now_bpm:.1f} BPM")
        self._status("Rendering transition with rubberband...")

        def _on_progress(p: float) -> None:
            # GIL-safe attribute write; the _tick() reader sees a consistent float.
            self._prep_progress = max(0.0, min(1.0, p))

        def _work():
            try:
                audio = self.engine.load_audio(next_path)
                cue_sample = int(cue_sec * SAMPLE_RATE)
                cue_audio = audio[cue_sample:]
                start_rate = now_bpm / next_bpm if next_bpm > 0 else 1.0
                buf = make_transition_buffer(
                    cue_audio, start_rate, fade_sec, restore_sec, SAMPLE_RATE,
                    progress_callback=_on_progress,
                )
                self._next_prepared = buf
                self._preparing = False
                self.call_from_thread(self._on_prepared)
            except Exception as exc:
                self._preparing = False
                self.call_from_thread(self._status, f"Prepare error: {exc}")

        threading.Thread(target=_work, daemon=True).start()

    def _on_prepared(self):
        self._prep_animating = False
        self._prep_progress = 1.0
        next_bpm = self._next_bpm
        now_bpm = self._now_bpm
        self.query_one("#next-track", NextTrackPanel).set_status(
            f"\\[READY - press M] {next_bpm:.1f}→{now_bpm:.1f} BPM"
        )
        self._status("Mix prepared.")

    def action_mix_now(self):
        if self._next_prepared is None:
            self._status("Not prepared yet. Press P to prepare.")
            return
        # Block mid-transition: the currently-playing buffer is either crossfading
        # or in the middle of its restoration ramp, so its current BPM doesn't match
        # the `start_rate` baked into the prepared buffer.
        if self.engine.state == State.MIXING or self._t_restore_start > 0.0:
            self._status("Cannot mix — previous transition still in progress.")
            return
        panel = self.query_one("#next-track", NextTrackPanel)

        # BPM-display animation metadata — covers the rate ramp inside the precomputed buffer
        self._restore_from_bpm = self._now_bpm
        self._restore_to_bpm = self._next_bpm
        self._restore_seconds = max(0.5, panel.restore)
        self._t_restore_start = 0.0   # armed by the MIXING→PLAYING transition in _tick()

        self.engine.start_mix(self._next_prepared, panel.fade)

        # Update now-playing metadata to next track
        self._now_path = self._next_path
        self._now_bpm = self._next_bpm
        self._now_key = self._next_key
        now_panel = self.query_one("#now-playing", NowPlayingPanel)
        now_panel.set_track(self._next_path, self._next_bpm, self._next_key)

        self.query_one("#next-track", NextTrackPanel).clear()
        self._next_path = None
        self._next_prepared = None
        self._status("Mixing...")

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
                self.query_one("#folder-tree", FolderTree).focus()
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
                    panel.set_cue(value)
                    self._status(f"Cue point set to {value:.1f}s")
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
            self._next_prepared = None
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
        current_state = self.engine.state

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

        # NowPlayingPanel BPM display
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
            panel.refresh_progress(self.engine.position, self.engine.duration, current_bpm)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _selected_song(self) -> Optional[str]:
        table = self.query_one("#song-list", DataTable)
        if not self._songs_in_view:
            return None
        row = table.cursor_row
        if row < 0 or row >= len(self._songs_in_view):
            return None
        return self._songs_in_view[row]

    def _do_load_now(self, path: str):
        bpm, key = self.library.get(path, (0.0, "?"))
        # Clear any in-flight transition state so the new track's BPM display isn't
        # hijacked by a prior mix's restoration ramp. Without this, the previous
        # transition's `_t_restore_start` and from/to BPMs keep driving the
        # NowPlayingPanel animation against the freshly loaded track. (Also
        # suppresses the spurious MIXING→PLAYING re-arming when this load
        # interrupts an active crossfade.)
        self._t_restore_start = 0.0
        self._restore_from_bpm = 0.0
        self._restore_to_bpm = 0.0
        self._status(f"Loading: {Path(path).name}...")

        def _work():
            try:
                audio = self.engine.load_audio(path)
                self._now_path = path
                self._now_bpm = bpm
                self._now_key = key
                self.engine.play(audio)
                self.call_from_thread(
                    lambda: (
                        self.query_one("#now-playing", NowPlayingPanel).set_track(path, bpm, key),
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
