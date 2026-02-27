"""App-level integration tests using Textual's run_test framework.

These tests start the real app in headless mode and drive it via Pilot.
Only the tmux server is mocked (external boundary) — all internal functions
run for real against a redirected state directory.
"""

import shutil

import git as gitpython
import pytest
from unittest.mock import MagicMock

from textual.widgets import Input

from super_worker.app import SuperWorkerApp, WorktreeTabContent
from super_worker.screens import (
    CommitMessageScreen,
    ConfigScreen,
    ConfirmDeleteScreen,
    NewSessionScreen,
    NewWorktreeScreen,
    RenameSessionScreen,
)
from super_worker.models import Worktree
from super_worker.widgets.sidebar import SessionDeleted
from super_worker.widgets.terminal_pane import TerminalPane


def _make_mock_server():
    """Create a mock libtmux server that satisfies all tmux operations."""
    mock_session = MagicMock()
    mock_session.session_name = "sw-test-0"
    mock_session.active_pane = MagicMock()
    mock_session.active_pane.capture_pane.return_value = ["test output"]
    mock_session.show_environment.return_value = {}

    mock_server = MagicMock()
    mock_server.sessions = [mock_session]
    mock_server.new_session.return_value = mock_session
    return mock_server


@pytest.fixture(autouse=True)
def isolate_externals(tmp_path, monkeypatch):
    """Mock only the tmux server and redirect state dir — everything else is real."""
    state_dir = tmp_path / "sw-state"
    state_dir.mkdir()
    monkeypatch.setattr("super_worker.services.state.STATE_DIR", state_dir)

    mock_server = _make_mock_server()
    monkeypatch.setattr("super_worker.services.tmux.libtmux.Server", lambda: mock_server)


@pytest.mark.asyncio
async def test_app_starts():
    """App starts without import errors or initialization crashes."""
    app = SuperWorkerApp()
    async with app.run_test() as pilot:
        assert app.is_running
        assert len(app._state.worktrees) >= 1


@pytest.mark.asyncio
async def test_new_worktree_modal_open_and_cancel():
    """Ctrl+N opens NewWorktreeScreen, Escape dismisses it."""
    app = SuperWorkerApp()
    async with app.run_test() as pilot:
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert isinstance(app.screen, NewWorktreeScreen)

        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, NewWorktreeScreen)


@pytest.mark.asyncio
async def test_new_worktree_creates_tab(monkeypatch):
    """Submitting NewWorktreeScreen creates a worktree and adds a tab."""
    app = SuperWorkerApp()
    async with app.run_test() as pilot:
        wt_dir = app._config.base_dir / f"{app._config.worktree_prefix}-test-feat"

        def fake_worktree_cmd(*args):
            if args[0] == "add":
                wt_dir.mkdir(parents=True, exist_ok=True)

        mock_repo = MagicMock()
        mock_repo.git.rev_parse.side_effect = gitpython.GitCommandError("rev-parse", 1)
        mock_repo.git.worktree.side_effect = fake_worktree_cmd
        monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)

        initial_count = len(app._state.worktrees)

        await pilot.press("ctrl+n")
        await pilot.pause()
        app.screen.query_one("#wt-name", Input).value = "test-feat"
        await pilot.press("enter")
        await pilot.pause(delay=2.0)

        assert len(app._state.worktrees) == initial_count + 1
        assert app._active_worktree is not None
        assert app._active_worktree.name == "test-feat"

        if wt_dir.exists():
            shutil.rmtree(wt_dir)


@pytest.mark.asyncio
async def test_new_session_creates_and_selects():
    """Creating a session adds it, activates it, and selects it in sidebar."""
    app = SuperWorkerApp()
    async with app.run_test() as pilot:
        wt = app._state.worktrees[0]
        app._active_worktree = wt
        initial_count = len(wt.sessions)

        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, NewSessionScreen)

        await pilot.press("enter")
        await pilot.pause(delay=2.0)

        assert len(wt.sessions) == initial_count + 1
        assert app._active_session_name == wt.sessions[-1].tmux_session_name

        # Terminal shows the new session
        wtc = app.query_one(f"#wtc-{wt.name}", WorktreeTabContent)
        terminal = wtc.query_one(TerminalPane)
        assert terminal.active_session == wt.sessions[-1].tmux_session_name


@pytest.mark.asyncio
async def test_new_session_cancel():
    """Escape dismisses NewSessionScreen without creating a session."""
    app = SuperWorkerApp()
    async with app.run_test() as pilot:
        wt = app._state.worktrees[0]
        app._active_worktree = wt
        initial_sessions = len(wt.sessions)

        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, NewSessionScreen)

        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, NewSessionScreen)
        assert len(wt.sessions) == initial_sessions


@pytest.mark.asyncio
async def test_rename_session():
    """Ctrl+R opens RenameSessionScreen and renaming updates the label."""
    app = SuperWorkerApp()
    async with app.run_test() as pilot:
        wt = app._state.worktrees[0]
        session = wt.sessions[0]
        app._active_worktree = wt
        app._active_session_name = session.tmux_session_name

        await pilot.press("ctrl+r")
        await pilot.pause()
        assert isinstance(app.screen, RenameSessionScreen)

        app.screen.query_one("#rename-input", Input).value = "renamed"
        await pilot.press("enter")
        await pilot.pause()

        assert session.label == "renamed"


@pytest.mark.asyncio
async def test_delete_main_worktree_blocked():
    """Cannot delete the main worktree."""
    app = SuperWorkerApp()
    async with app.run_test() as pilot:
        app._active_worktree = app._state.worktrees[0]
        await pilot.press("ctrl+d")
        await pilot.pause()
        assert not isinstance(app.screen, ConfirmDeleteScreen)


@pytest.mark.asyncio
async def test_delete_worktree_opens_confirm():
    """Ctrl+D opens ConfirmDeleteScreen for non-main worktrees."""
    app = SuperWorkerApp()
    async with app.run_test() as pilot:
        wt = Worktree(name="feature", path=str(app._config.repo_root), branch="sw-feature")
        app._state.worktrees.append(wt)
        app._active_worktree = wt

        await pilot.press("ctrl+d")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmDeleteScreen)


@pytest.mark.asyncio
async def test_delete_only_session_clears_terminal():
    """Deleting the sole session removes it from state and clears the terminal."""
    app = SuperWorkerApp()
    async with app.run_test() as pilot:
        wt = app._state.worktrees[0]
        session = wt.sessions[0]
        app._active_worktree = wt
        app._active_session_name = session.tmux_session_name

        wtc = app.query_one(f"#wtc-{wt.name}", WorktreeTabContent)
        terminal = wtc.query_one(TerminalPane)
        terminal.active_session = session.tmux_session_name
        await pilot.pause()

        app.post_message(SessionDeleted(wt, session))
        await pilot.pause(delay=1.0)

        assert len(wt.sessions) == 0
        assert terminal.active_session is None
        assert app._active_session_name is None


@pytest.mark.asyncio
async def test_delete_session_auto_selects_another():
    """Deleting a session when others remain auto-selects the first remaining session."""
    app = SuperWorkerApp()
    async with app.run_test() as pilot:
        wt = app._state.worktrees[0]
        app._active_worktree = wt

        # Create a second session
        await pilot.press("ctrl+s")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause(delay=2.0)

        assert len(wt.sessions) == 2
        first_session = wt.sessions[0]
        second_session = wt.sessions[1]

        wtc = app.query_one(f"#wtc-{wt.name}", WorktreeTabContent)
        terminal = wtc.query_one(TerminalPane)
        terminal.active_session = first_session.tmux_session_name
        app._active_session_name = first_session.tmux_session_name

        app.post_message(SessionDeleted(wt, first_session))
        await pilot.pause(delay=1.0)

        assert len(wt.sessions) == 1
        assert app._active_session_name == second_session.tmux_session_name
        assert terminal.active_session == second_session.tmux_session_name


@pytest.mark.asyncio
async def test_commit_dialog_opens():
    """Git commit action opens the CommitMessageScreen."""
    app = SuperWorkerApp()
    async with app.run_test() as pilot:
        wt = app._state.worktrees[0]
        app._git_commit(wt)
        await pilot.pause()
        assert isinstance(app.screen, CommitMessageScreen)


@pytest.mark.asyncio
async def test_settings_modal_opens():
    """Ctrl+E opens ConfigScreen."""
    app = SuperWorkerApp()
    async with app.run_test() as pilot:
        await pilot.press("ctrl+e")
        await pilot.pause()
        assert isinstance(app.screen, ConfigScreen)


@pytest.mark.asyncio
@pytest.mark.parametrize("key,active_wt,active_session,screen_type", [
    ("ctrl+r", True, False, RenameSessionScreen),
    ("ctrl+a", False, False, None),
])
async def test_no_active_session_does_not_crash(key, active_wt, active_session, screen_type):
    """Actions requiring an active session warn gracefully."""
    app = SuperWorkerApp()
    async with app.run_test() as pilot:
        if not active_wt:
            app._active_worktree = None
        app._active_session_name = None
        await pilot.press(key)
        await pilot.pause()
        if screen_type:
            assert not isinstance(app.screen, screen_type)
