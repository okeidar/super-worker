"""Tests for SessionSidebar.show_worktree() using Textual's run_test pattern."""

import pytest
from textual.app import App, ComposeResult
from textual.widgets import ListView

from super_worker.models import Session, Worktree
from super_worker.services.tmux import SessionState
from super_worker.widgets.sidebar import SessionSidebar


class SidebarTestApp(App):
    def compose(self) -> ComposeResult:
        yield SessionSidebar()


def _make_worktree(session_labels: list[str]) -> Worktree:
    sessions = [
        Session(tmux_session_name=f"sw-test-{i}", label=label)
        for i, label in enumerate(session_labels)
    ]
    return Worktree(name="test-wt", path="/tmp/test", branch="main", sessions=sessions)


def _states(wt: Worktree, state: SessionState = SessionState.RUNNING) -> dict[str, SessionState]:
    return {s.tmux_session_name: state for s in wt.sessions}


_GIT = {"ahead": 0, "behind": 0}


@pytest.mark.asyncio
async def test_show_worktree_populates_list():
    """show_worktree with 2 sessions produces 2 items in the ListView."""
    wt = _make_worktree(["alpha", "beta"])
    app = SidebarTestApp()
    async with app.run_test() as pilot:
        sidebar = app.query_one(SessionSidebar)
        sidebar.show_worktree(wt, states=_states(wt), git_status=_GIT, git_dirty=False)
        await pilot.pause()

        assert len(sidebar.query_one("#session-list", ListView).children) == 2


@pytest.mark.asyncio
async def test_show_worktree_updates_in_place():
    """Calling show_worktree twice with different states updates labels without adding items."""
    wt = _make_worktree(["alpha", "beta"])
    app = SidebarTestApp()
    async with app.run_test() as pilot:
        sidebar = app.query_one(SessionSidebar)

        sidebar.show_worktree(wt, states=_states(wt, SessionState.RUNNING), git_status=_GIT, git_dirty=False)
        await pilot.pause()

        sidebar.show_worktree(wt, states=_states(wt, SessionState.WAITING_INPUT), git_status=_GIT, git_dirty=False)
        await pilot.pause()

        assert len(sidebar.query_one("#session-list", ListView).children) == 2


@pytest.mark.asyncio
async def test_show_worktree_grows_and_shrinks():
    """List correctly adds items then removes excess items."""
    app = SidebarTestApp()
    async with app.run_test() as pilot:
        sidebar = app.query_one(SessionSidebar)
        sess_list = sidebar.query_one("#session-list", ListView)

        # Start with 1, grow to 3
        wt1 = _make_worktree(["a"])
        sidebar.show_worktree(wt1, states=_states(wt1), git_status=_GIT, git_dirty=False)
        await pilot.pause()
        assert len(sess_list.children) == 1

        wt3 = _make_worktree(["a", "b", "c"])
        sidebar.show_worktree(wt3, states=_states(wt3), git_status=_GIT, git_dirty=False)
        await pilot.pause()
        assert len(sess_list.children) == 3

        # Shrink back to 1
        sidebar.show_worktree(wt1, states=_states(wt1), git_status=_GIT, git_dirty=False)
        await pilot.pause()
        assert len(sess_list.children) == 1


@pytest.mark.asyncio
async def test_show_worktree_skips_rebuild_when_unchanged():
    """Calling show_worktree twice with identical data skips the list rebuild."""
    wt = _make_worktree(["alpha", "beta"])
    states = _states(wt)
    app = SidebarTestApp()
    async with app.run_test() as pilot:
        sidebar = app.query_one(SessionSidebar)

        sidebar.show_worktree(wt, states=states, git_status=_GIT, git_dirty=False)
        await pilot.pause()

        list_view = sidebar.query_one("#session-list", ListView)
        items_before = list(list_view.children)

        sidebar.show_worktree(wt, states=states, git_status=_GIT, git_dirty=False)
        await pilot.pause()

        # Item references must be identical (no rebuild)
        assert list(list_view.children) == items_before


@pytest.mark.asyncio
async def test_session_map_tracks_sessions():
    """_session_map maps each index to the corresponding Session object."""
    wt = _make_worktree(["alpha", "beta", "gamma"])
    app = SidebarTestApp()
    async with app.run_test() as pilot:
        sidebar = app.query_one(SessionSidebar)
        sidebar.show_worktree(wt, states=_states(wt), git_status=_GIT, git_dirty=False)
        await pilot.pause()

        assert len(sidebar._session_map) == 3
        for i, session in enumerate(wt.sessions):
            assert sidebar._session_map[i] is session
