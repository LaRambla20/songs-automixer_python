"""Folder-tree navigation contract: drill-down, accordion, and collapse cascade.

Unlike the other app probes (which use the AutoMixApp.__new__ harness), tree
navigation needs a real mounted widget tree -- focus moves, Tree.NodeSelected
messages, and line layout only exist once mounted. So this drives the actual app
headlessly via Textual's run_test() Pilot, with the AudioEngine/CuePlayer stubbed
(and _tick neutered) so no audio device is touched.

Covers:
  * right-arrow / Enter three-level drill-down (root display -> subfolders -> songs)
  * accordion: opening a subfolder collapses an open sibling (right-arrow AND Enter)
  * cascade: collapsing an ancestor also collapses hidden descendants (the
    _collapse_subtree fix -- no stale 'v' grandchild reappears on re-open)
  * empty (song-less) subfolder keeps focus on the tree, not an empty table
  * left-arrow exit cascades; up-arrow never re-lands on the root; second
    activation toggles a folder closed
"""
import asyncio
import contextlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import automix.app as app_mod
from automix.audio_engine import State


class FakeEngine:
    """No-op stand-in so mounting never opens a real output stream."""
    def __init__(self, *a, **k):
        self.state = State.IDLE

    def load_audio(self, path):           # called by _preload_backspin (caught)
        raise RuntimeError("stub engine: no decode in nav test")

    def close(self):                      # on_unmount
        pass

    def stop(self):
        pass


# Patch the module globals the app constructor / mount look up. _tick is neutered
# because it reads many engine fields we don't stub and is irrelevant to nav.
app_mod.AudioEngine = FakeEngine
app_mod.CuePlayer = lambda *a, **k: None
app_mod.AutoMixApp._tick = lambda self: None

from automix.app import AutoMixApp, FolderTree   # noqa: E402
from textual.widgets import DataTable            # noqa: E402

BACKSPIN = str(Path(__file__).parent.parent / "samples" / "top_DJ_Rewind_SFX_10.mp3")


def _make_tree(root):
    """root/A/{A1/deep.mp3, a.mp3}, root/B/b.mp3, root/C/C1/c1.mp3 (C songless)."""
    def touch(p):
        open(p, "wb").close()
    os.makedirs(os.path.join(root, "A", "A1"))
    touch(os.path.join(root, "A", "a.mp3"))
    touch(os.path.join(root, "A", "A1", "deep.mp3"))
    os.makedirs(os.path.join(root, "B"))
    touch(os.path.join(root, "B", "b.mp3"))
    os.makedirs(os.path.join(root, "C", "C1"))
    touch(os.path.join(root, "C", "C1", "c1.mp3"))   # C has no *direct* song


@contextlib.asynccontextmanager
async def mounted_app():
    tmp = tempfile.mkdtemp(prefix="navtest_")
    _make_tree(tmp)
    app = AutoMixApp(root_folder=tmp, library={}, backspin_sample=BACKSPIN)
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            yield app, pilot
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _node(tree, label):
    found = []

    def walk(n):
        if str(n.label) == label:
            found.append(n)
        for c in n.children:
            walk(c)
    walk(tree.root)
    assert found, f"node {label!r} not found"
    return found[0]


def _expansion(tree):
    """{label: is_expanded} for every expandable node, visible or hidden."""
    out = {}

    def walk(n):
        if n.allow_expand:
            out[str(n.label)] = n.is_expanded
        for c in n.children:
            walk(c)
    walk(tree.root)
    return out


async def _focus_on(app, label):
    """Put the tree cursor on a (currently visible) node and focus the tree."""
    tree = app.query_one("#folder-tree", FolderTree)
    tree.focus()
    node = _node(tree, label)
    assert node.line >= 0, f"node {label!r} is not visible (line={node.line})"
    tree.cursor_line = node.line
    await asyncio.sleep(0)


def _focused_name(app):
    return type(app.focused).__name__ if app.focused else "None"


# ---------------------------------------------------------------------------

async def test_initial_state():
    """Opens on the root display: root collapsed, focus on the tree, no songs."""
    async with mounted_app() as (app, pilot):
        tree = app.query_one("#folder-tree", FolderTree)
        assert not tree.root.is_expanded, "root should start collapsed"
        assert _focused_name(app) == "FolderTree"
        assert app.query_one("#song-list", DataTable).row_count == 0
    print("initial root display: PASS")


async def test_right_arrow_drilldown():
    """right on root -> reveal subfolders (cursor to first child, focus stays);
    right on a subfolder -> load songs + focus the table."""
    async with mounted_app() as (app, pilot):
        tree = app.query_one("#folder-tree", FolderTree)
        await pilot.press("right")                       # expand root
        await pilot.pause()
        assert tree.root.is_expanded, "root should expand"
        assert tree.cursor_line == tree.root.line + 1, "cursor should move to first child"
        assert _focused_name(app) == "FolderTree", "root expand keeps focus on tree"

        await _focus_on(app, "A")
        await pilot.press("right")                        # open subfolder A
        await pilot.pause()
        assert _expansion(tree)["A"] is True
        assert _focused_name(app) == "SongTable", "opening a song folder focuses the table"
        assert app.query_one("#song-list", DataTable).row_count == 1
    print("right-arrow drill-down (root -> subfolder -> songs): PASS")


async def test_accordion_cascade_right():
    """right-arrow: open A, open grandchild A1, then open sibling B. Accordion must
    collapse A, and the cascade must collapse the hidden grandchild A1 too."""
    async with mounted_app() as (app, pilot):
        tree = app.query_one("#folder-tree", FolderTree)
        await pilot.press("right")                        # root
        await pilot.pause()
        await _focus_on(app, "A"); await pilot.press("right")   # open A
        await pilot.pause()
        await _focus_on(app, "A1"); await pilot.press("right")  # open grandchild A1
        await pilot.pause()
        exp = _expansion(tree)
        assert exp["A"] and exp["A1"], f"A and A1 should be open, got {exp}"

        await _focus_on(app, "B"); await pilot.press("right")   # accordion + cascade
        await pilot.pause()
        exp = _expansion(tree)
        assert exp["A"] is False, f"sibling A should collapse (accordion), got {exp}"
        assert exp["A1"] is False, f"grandchild A1 should collapse (cascade), got {exp}"
        assert exp["B"] is True, f"B should be open, got {exp}"
    print("right-arrow accordion + grandchild cascade: PASS")


async def test_accordion_cascade_enter():
    """Same accordion + cascade via Enter (on_tree_node_selected path)."""
    async with mounted_app() as (app, pilot):
        tree = app.query_one("#folder-tree", FolderTree)
        await pilot.press("right")
        await pilot.pause()
        await _focus_on(app, "A"); await pilot.press("enter")
        await pilot.pause()
        await _focus_on(app, "A1"); await pilot.press("enter")
        await pilot.pause()
        assert _expansion(tree)["A1"], "A1 should be open before accordion"

        await _focus_on(app, "B"); await pilot.press("enter")
        await pilot.pause()
        exp = _expansion(tree)
        assert exp["A"] is False and exp["A1"] is False and exp["B"] is True, \
            f"Enter accordion/cascade failed: {exp}"
    print("Enter accordion + grandchild cascade: PASS")


async def test_empty_folder_keeps_focus():
    """right-arrow on a subfolder with no direct songs keeps focus on the tree
    (don't jump into an empty DataTable)."""
    async with mounted_app() as (app, pilot):
        await pilot.press("right")
        await pilot.pause()
        await _focus_on(app, "C"); await pilot.press("right")    # C has C1 but no songs
        await pilot.pause()
        assert app.query_one("#song-list", DataTable).row_count == 0, "C has no direct songs"
        assert _focused_name(app) == "FolderTree", "empty folder must keep focus on tree"
    print("song-less folder keeps focus on tree: PASS")


async def test_left_arrow_exit_cascade():
    """Open A then A1, then left-arrow from the song table: exits to the tree and
    collapses the whole A subtree (A and A1)."""
    async with mounted_app() as (app, pilot):
        tree = app.query_one("#folder-tree", FolderTree)
        await pilot.press("right")
        await pilot.pause()
        await _focus_on(app, "A"); await pilot.press("enter")    # open A, focus table
        await pilot.pause()
        await _focus_on(app, "A1"); await pilot.press("enter")   # open A1, focus table
        await pilot.pause()
        # left from the song table exits browsing; cursor_node is A1 here.
        assert _focused_name(app) == "SongTable"
        await pilot.press("left")
        await pilot.pause()
        exp = _expansion(tree)
        assert exp["A1"] is False, f"A1 should collapse on exit, got {exp}"
        assert _focused_name(app) == "FolderTree", "left-arrow returns focus to tree"
    print("left-arrow exit cascade: PASS")


async def test_up_arrow_skips_root():
    """Once subfolders are shown, up-arrow never re-lands the cursor on the root."""
    async with mounted_app() as (app, pilot):
        tree = app.query_one("#folder-tree", FolderTree)
        await pilot.press("right")                        # root expands, cursor -> child 1
        await pilot.pause()
        assert tree.cursor_line == tree.root.line + 1
        await pilot.press("up")                            # would land on root -> blocked
        await pilot.pause()
        assert tree.cursor_line == tree.root.line + 1, "up must not move cursor onto root"
    print("up-arrow never lands on root: PASS")


async def test_left_from_subfolder_returns_to_root_display():
    """From a first-level subfolder, one left press collapses the root (back to
    root display) and cascades any open child."""
    async with mounted_app() as (app, pilot):
        tree = app.query_one("#folder-tree", FolderTree)
        await pilot.press("right")
        await pilot.pause()
        await _focus_on(app, "A")
        await pilot.press("left")                          # parent (root) collapses
        await pilot.pause()
        assert not tree.root.is_expanded, "left from first-level subfolder collapses root"
        assert tree.cursor_line == 0, "cursor returns to the root row"
    print("left-arrow returns to root display: PASS")


async def test_second_activation_toggles_closed():
    """A second activation on an already-open folder exits/collapses it."""
    async with mounted_app() as (app, pilot):
        tree = app.query_one("#folder-tree", FolderTree)
        # Root toggle: expand, then re-activate the root row -> back to root display.
        await pilot.press("right")
        await pilot.pause()
        tree.focus(); tree.cursor_line = tree.root.line
        await asyncio.sleep(0)
        await pilot.press("enter")                         # second activation on root
        await pilot.pause()
        assert not tree.root.is_expanded, "re-activating an open root collapses it"

        # Empty-subfolder toggle: open C (focus stays on tree), Enter again -> close.
        await pilot.press("right")                         # re-expand root
        await pilot.pause()
        await _focus_on(app, "C"); await pilot.press("enter")   # open empty C
        await pilot.pause()
        assert _expansion(tree)["C"] is True and _focused_name(app) == "FolderTree"
        await _focus_on(app, "C"); await pilot.press("enter")   # second activation -> close
        await pilot.pause()
        assert _expansion(tree)["C"] is False, "second activation should collapse C"
    print("second activation toggles a folder closed: PASS")


async def main():
    await test_initial_state()
    await test_right_arrow_drilldown()
    await test_accordion_cascade_right()
    await test_accordion_cascade_enter()
    await test_empty_folder_keeps_focus()
    await test_left_arrow_exit_cascade()
    await test_up_arrow_skips_root()
    await test_left_from_subfolder_returns_to_root_display()
    await test_second_activation_toggles_closed()
    print("\nAll tree-nav tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
