"""Tests for the preview terminal rendering: color preservation + cursor overlay."""

from super_worker.services.tmux import PaneSnapshot
from super_worker.widgets.terminal_pane import render_snapshot


def _span_styles(text):
    return [s.style for s in text.spans]


def test_background_color_preserved():
    """Backgrounds (e.g. diff highlights) must survive rendering, not be stripped."""
    snap = PaneSnapshot(text="\x1b[42mADDED\x1b[0m")  # green background
    text = render_snapshot(snap)
    assert text.plain == "ADDED"
    has_bg = any(getattr(s.style, "bgcolor", None) is not None for s in text.spans)
    assert has_bg, "background color was stripped from the rendered preview"


def test_foreground_color_preserved():
    snap = PaneSnapshot(text="\x1b[31mRED\x1b[0m")
    text = render_snapshot(snap)
    assert text.plain == "RED"
    has_fg = any(getattr(s.style, "color", None) is not None for s in text.spans)
    assert has_fg


def test_cursor_overlay_within_content():
    """Cursor inside the line content gets a reverse-video cell."""
    snap = PaneSnapshot(text="abcdef", cursor_x=2, cursor_y=0, pane_height=1, cursor_visible=True)
    text = render_snapshot(snap)
    assert text.plain == "abcdef"
    assert any(
        s.start == 2 and s.end == 3 and s.style == "reverse" for s in text.spans
    ), "expected a reverse-video cursor at column 2"


def test_cursor_overlay_at_end_of_prompt():
    """Cursor past the end of the last line extends it so the block is visible."""
    snap = PaneSnapshot(text="> ", cursor_x=2, cursor_y=0, pane_height=1, cursor_visible=True)
    text = render_snapshot(snap)
    assert text.plain == ">  ", "cursor cell should extend the empty prompt line"
    assert "reverse" in _span_styles(text)


def test_cursor_past_trimmed_mid_line():
    """tmux trims trailing spaces: a cursor past a MIDDLE line's end must still draw.

    This is Claude Code's normal state — the input row sits above status
    lines, and its trailing spaces are trimmed in the capture.
    """
    snap = PaneSnapshot(
        text="❯\nstatus line\nmode line",
        cursor_x=2,   # past the trimmed "❯" line end
        cursor_y=0,
        pane_height=3,
        history_size=0,
        cursor_visible=True,
    )
    text = render_snapshot(snap)
    assert "reverse" in _span_styles(text), "caret must draw on a padded middle line"
    lines = text.plain.split("\n")
    assert lines[0] == "❯  ", "line with cursor should be padded to the cursor column"
    assert lines[1] == "status line", "following lines must be unchanged"


def test_cursor_line_is_cursor_y():
    """Snapshots are visible-only: the cursor's line is simply cursor_y."""
    snap = PaneSnapshot(
        text="visible0\nHERE",
        cursor_x=0,
        cursor_y=1,
        pane_height=2,
        cursor_visible=True,
    )
    text = render_snapshot(snap)
    offset = len("visible0\n")
    assert any(
        s.start == offset and s.end == offset + 1 and s.style == "reverse"
        for s in text.spans
    ), "cursor landed on the wrong line"


def test_cursor_below_trimmed_lines():
    """Cursor on a blank row below the last captured line: lines are padded."""
    # Screen is 5 rows; capture trimmed to 1 line; cursor parked on row 3.
    snap = PaneSnapshot(
        text="only line",
        cursor_x=0,
        cursor_y=3,
        pane_height=5,
        cursor_visible=True,
    )
    text = render_snapshot(snap)
    assert "reverse" in _span_styles(text), "caret must draw below trimmed content"
    assert text.plain.count("\n") == 3, "blank lines should be padded up to the cursor row"


def test_cursor_on_wide_char_line():
    """Cell column maps to character index correctly with wide (2-cell) chars."""
    # "你好" occupies 4 cells; cursor at cell 4 = char index 2 (the 'x')
    snap = PaneSnapshot(text="你好x", cursor_x=4, cursor_y=0, pane_height=1, cursor_visible=True)
    text = render_snapshot(snap)
    assert any(
        s.start == 2 and s.end == 3 and s.style == "reverse" for s in text.spans
    ), "wide chars threw off the cursor column"


def test_no_cursor_when_hidden():
    snap = PaneSnapshot(text="abc", cursor_x=1, cursor_y=0, pane_height=1, cursor_visible=False)
    text = render_snapshot(snap)
    assert "reverse" not in _span_styles(text)


def test_no_cursor_when_pane_height_unknown():
    snap = PaneSnapshot(text="abc", cursor_x=1, cursor_y=0, pane_height=0, cursor_visible=True)
    text = render_snapshot(snap)
    assert "reverse" not in _span_styles(text)


# ── History accumulation (mounted widgets mirror the chunk cache) ────────────

import pytest
from rich.text import Text as _Text
from textual.app import App as _App, ComposeResult as _CR
from textual.containers import VerticalScroll as _VS
from super_worker.widgets.terminal_pane import TerminalPane as _TP
from super_worker.constants import (
    PANE_HISTORY_MAX_LINES as _MAX,
    PANE_HISTORY_CHUNK_LINES as _CHUNK,
)


class _HostApp(_App):
    def compose(self) -> _CR:
        yield _TP()


def _feed(pane, session, start, n):
    lines = "\n".join(f"L{start + i}" for i in range(n))
    pane._append_history(session, _Text(lines))
    return start + n


@pytest.mark.asyncio
async def test_history_mirror_consistent_after_prune():
    """After pruning, mounted widgets exactly mirror the chunk cache — no dup/gap."""
    app = _HostApp()
    async with app.run_test(size=(100, 30)) as pilot:
        p = app.query_one(_TP)
        p._paused = False
        p.set_reactive(_TP.active_session, "s")
        await pilot.pause(delay=0.1)

        counter = 0
        for _ in range(200):  # 200*37 = 7400 lines > cap, forces prune
            counter = _feed(p, "s", counter, 37)
        await pilot.pause(delay=0.1)

        st = p._hist["s"]
        mounted = list(app.query_one("#terminal-scroll", _VS).query(".hist-chunk"))
        assert sum(st.counts) == st.total
        assert len(st.chunks) == len(st.counts) == len(st.widgets) == len(mounted)
        assert st.total <= _MAX + _CHUNK
        nums = [int(x[1:]) for x in "\n".join(c.plain for c in st.chunks).split("\n") if x.startswith("L")]
        assert nums == list(range(nums[0], nums[-1] + 1)), "duplicated or lost history lines"
        assert nums[-1] == counter - 1, "newest line missing from scrollback"
        assert [w.render().plain for w in mounted] == [c.plain for c in st.chunks]


@pytest.mark.asyncio
async def test_history_swaps_on_session_switch(monkeypatch):
    """Switching active_session shows that session's own scrollback, not the other's.

    Uses normal attribute assignment (not set_reactive) so the real
    watch_active_session → _remount_history wiring is exercised. tmux is
    mocked so the poll/resize side effects stay hermetic.
    """
    import super_worker.widgets.terminal_pane as tp_mod
    monkeypatch.setattr(tp_mod, "capture_pane_snapshot",
                        lambda name: PaneSnapshot(text=""))
    monkeypatch.setattr(tp_mod, "set_window_size", lambda *a, **k: None)
    monkeypatch.setattr(tp_mod, "resize_window", lambda *a, **k: None)

    app = _HostApp()
    async with app.run_test(size=(100, 30)) as pilot:
        p = app.query_one(_TP)
        p._paused = False
        p.active_session = "A"          # normal assignment → fires the watcher
        await pilot.pause(delay=0.1)
        _feed(p, "A", 0, 50)
        await pilot.pause(delay=0.1)

        # Switch to B — A's chunks must leave the DOM, B starts empty
        p.active_session = "B"
        await pilot.pause(delay=0.1)
        mounted_b = list(app.query_one("#terminal-scroll", _VS).query(".hist-chunk"))
        assert mounted_b == [], "B should start with no mounted history"
        _feed(p, "B", 1000, 30)
        await pilot.pause(delay=0.1)
        text_b = "\n".join(w.render().plain for w in app.query(".hist-chunk"))
        assert "L1000" in text_b and "L0" not in text_b, "B must not show A's history"

        # Switch back to A — its 50 lines come back intact
        p.active_session = "A"
        await pilot.pause(delay=0.1)
        text_a = "\n".join(w.render().plain for w in app.query(".hist-chunk"))
        assert "L0" in text_a and "L49" in text_a and "L1000" not in text_a
