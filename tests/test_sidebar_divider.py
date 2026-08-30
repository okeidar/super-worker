"""Tests for the draggable SidebarDivider that resizes the session sidebar."""

import pytest
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Static

from super_worker.widgets.sidebar import SessionSidebar, SidebarDivider


class _Host(App):
    CSS = "#term { width: 1fr; height: 1fr; }"

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield SessionSidebar()
            yield SidebarDivider()
            yield Static("term", id="term")


class _FakeMouse:
    """Minimal stand-in for a Textual MouseEvent (only what the divider reads)."""

    def __init__(self, screen_x: int) -> None:
        self.screen_x = screen_x

    def stop(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _reset_shared_width():
    # The chosen width is class-level state; keep tests independent.
    SidebarDivider._shared_width = None
    yield
    SidebarDivider._shared_width = None


@pytest.mark.asyncio
async def test_drag_resizes_sidebar():
    app = _Host()
    async with app.run_test(size=(120, 30)) as pilot:
        divider = app.query_one(SidebarDivider)
        sidebar = app.query_one(SessionSidebar)
        assert sidebar.region.width == 32, "default width from CSS"

        # The container is the top-level Horizontal at screen x=0, so the new
        # sidebar width is simply the pointer's absolute x.
        divider.on_mouse_down(_FakeMouse(50))
        divider.on_mouse_move(_FakeMouse(50))
        await pilot.pause()
        assert sidebar.region.width == 50
        assert SidebarDivider._shared_width == 50


@pytest.mark.asyncio
async def test_drag_clamps_to_bounds():
    app = _Host()
    async with app.run_test(size=(120, 30)) as pilot:
        divider = app.query_one(SidebarDivider)
        sidebar = app.query_one(SessionSidebar)

        # Too narrow → clamped up to MIN_WIDTH.
        divider.on_mouse_down(_FakeMouse(2))
        divider.on_mouse_move(_FakeMouse(2))
        await pilot.pause()
        assert sidebar.region.width == SidebarDivider.MIN_WIDTH

        # Too wide → clamped so the terminal keeps _MIN_TERMINAL columns.
        divider.on_mouse_move(_FakeMouse(500))
        await pilot.pause()
        assert sidebar.region.width == 120 - SidebarDivider._MIN_TERMINAL

        divider.on_mouse_up(_FakeMouse(500))


@pytest.mark.asyncio
async def test_shared_width_applied_on_mount():
    """A width chosen earlier is adopted by a freshly-mounted divider's sidebar."""
    SidebarDivider._shared_width = 44
    app = _Host()
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        sidebar = app.query_one(SessionSidebar)
        assert sidebar.region.width == 44
