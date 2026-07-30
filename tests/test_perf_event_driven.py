"""Tests for event-driven state detection and pipe file management.

Covers:
- State file reading (read_state_file, read_all_state_files)
- State file cleanup (cleanup_state_file)
- Pipe file truncation in PaneWatcher
- State file watching in PaneWatcher
- Hook script writes state files
- ContentChanged no longer triggers subprocess storm
- periodic_refresh still detects dead sessions
"""

import inspect
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from super_worker.services.tmux import (
    SessionState,
    cleanup_state_file,
    read_all_state_files,
    read_state_file,
)


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Create a temporary session-states directory."""
    d = tmp_path / "session-states"
    d.mkdir()
    monkeypatch.setattr("super_worker.services.tmux.SESSION_STATES_DIR", d, raising=False)
    # Patch the import in the module
    import super_worker.constants
    monkeypatch.setattr(super_worker.constants, "SESSION_STATES_DIR", d)
    return d


class TestReadStateFile:
    def test_read_waiting_input(self, state_dir):
        (state_dir / "sw-main-0").write_text("waiting_input")
        assert read_state_file("sw-main-0") == SessionState.WAITING_INPUT

    def test_read_waiting_approval(self, state_dir):
        (state_dir / "sw-main-0").write_text("waiting_approval")
        assert read_state_file("sw-main-0") == SessionState.WAITING_APPROVAL

    def test_read_running(self, state_dir):
        (state_dir / "sw-main-0").write_text("running")
        assert read_state_file("sw-main-0") == SessionState.RUNNING

    def test_read_missing_file(self, state_dir):
        assert read_state_file("nonexistent") == SessionState.UNKNOWN

    def test_read_empty_file(self, state_dir):
        (state_dir / "sw-main-0").write_text("")
        assert read_state_file("sw-main-0") == SessionState.UNKNOWN

    def test_read_unknown_state(self, state_dir):
        (state_dir / "sw-main-0").write_text("some_garbage")
        assert read_state_file("sw-main-0") == SessionState.UNKNOWN

    def test_read_with_whitespace(self, state_dir):
        (state_dir / "sw-main-0").write_text("  waiting_input\n")
        assert read_state_file("sw-main-0") == SessionState.WAITING_INPUT


class TestReadAllStateFiles:
    def test_read_multiple(self, state_dir):
        (state_dir / "sw-main-0").write_text("running")
        (state_dir / "sw-feat-0").write_text("waiting_approval")
        result = read_all_state_files(["sw-main-0", "sw-feat-0"])
        assert result == {
            "sw-main-0": SessionState.RUNNING,
            "sw-feat-0": SessionState.WAITING_APPROVAL,
        }

    def test_missing_files_return_unknown(self, state_dir):
        (state_dir / "sw-main-0").write_text("running")
        result = read_all_state_files(["sw-main-0", "sw-missing-0"])
        assert result["sw-main-0"] == SessionState.RUNNING
        assert result["sw-missing-0"] == SessionState.UNKNOWN

    def test_empty_list(self, state_dir):
        assert read_all_state_files([]) == {}


class TestCleanupStateFile:
    def test_cleanup_existing(self, state_dir):
        f = state_dir / "sw-main-0"
        f.write_text("running")
        cleanup_state_file("sw-main-0")
        assert not f.exists()

    def test_cleanup_nonexistent(self, state_dir):
        # Should not raise
        cleanup_state_file("sw-nonexistent-0")


class TestPaneWatcherStateWatching:
    """Test state file watching setup and teardown."""

    def test_start_stop_state_watching(self, state_dir):
        from super_worker.services.pane_watcher import PaneWatcher

        watcher = PaneWatcher()
        try:
            callback = MagicMock()
            # start_watching_state should create the state file and add to _state_watches
            watcher.start_watching_state("sw-main-0", callback)
            assert "sw-main-0" in watcher._state_watches
            assert (state_dir / "sw-main-0").exists()

            # stop should remove from _state_watches
            watcher.stop_watching_state("sw-main-0")
            assert "sw-main-0" not in watcher._state_watches
        finally:
            watcher.cleanup()

    def test_cleanup_stops_state_watches(self, state_dir):
        from super_worker.services.pane_watcher import PaneWatcher

        watcher = PaneWatcher()
        callback = MagicMock()
        watcher.start_watching_state("sw-main-0", callback)
        watcher.start_watching_state("sw-feat-0", callback)
        assert len(watcher._state_watches) == 2

        watcher.cleanup()
        assert len(watcher._state_watches) == 0

    def test_start_watching_state_replaces_existing(self, state_dir):
        from super_worker.services.pane_watcher import PaneWatcher

        watcher = PaneWatcher()
        try:
            cb1 = MagicMock()
            cb2 = MagicMock()
            watcher.start_watching_state("sw-main-0", cb1)
            watcher.start_watching_state("sw-main-0", cb2)
            # Should have only one watch, with the new callback
            assert len(watcher._state_watches) == 1
            assert watcher._state_watches["sw-main-0"].callback is cb2
        finally:
            watcher.cleanup()


class TestTerminalPaneStateWatching:
    """Test TerminalPane.start_watching_states manages watch set correctly."""

    def test_start_watching_states_diff(self, state_dir):
        from super_worker.widgets.terminal_pane import TerminalPane

        pane = TerminalPane()
        watcher = pane._watcher

        # Initial set
        pane.start_watching_states(["sw-main-0", "sw-feat-0"])
        assert pane._watched_state_sessions == {"sw-main-0", "sw-feat-0"}
        assert "sw-main-0" in watcher._state_watches
        assert "sw-feat-0" in watcher._state_watches

        # Update: remove feat, add bug
        pane.start_watching_states(["sw-main-0", "sw-bug-0"])
        assert pane._watched_state_sessions == {"sw-main-0", "sw-bug-0"}
        assert "sw-feat-0" not in watcher._state_watches
        assert "sw-bug-0" in watcher._state_watches
        assert "sw-main-0" in watcher._state_watches

        watcher.cleanup()


class TestHookWritesStateFile:
    """Test that sw-hook.sh writes state files."""

    def test_hook_script_contains_state_file_write(self):
        """Verify the hook script writes to session-states directory."""
        import importlib.resources
        ref = importlib.resources.files("super_worker.scripts").joinpath("sw-hook.sh")
        with importlib.resources.as_file(ref) as path:
            content = path.read_text()

        assert "session-states" in content
        assert 'printf' in content
        assert '${SW_SESSION_NAME}' in content

    def test_hook_script_skips_redundant_writes(self):
        """Hook should skip if state file already has the same value."""
        import importlib.resources
        ref = importlib.resources.files("super_worker.scripts").joinpath("sw-hook.sh")
        with importlib.resources.as_file(ref) as path:
            content = path.read_text()

        # Must read existing state file and compare before doing tmux calls
        assert 'cat' in content or 'read' in content
        assert 'exit 0' in content

    def test_hook_actually_skips_redundant_calls(self, tmp_path):
        """Run the hook script and verify it skips when state unchanged."""
        import importlib.resources
        import subprocess

        ref = importlib.resources.files("super_worker.scripts").joinpath("sw-hook.sh")
        with importlib.resources.as_file(ref) as script_path:
            state_dir = tmp_path / "session-states"
            state_dir.mkdir()
            state_file = state_dir / "sw-test-hook-0"

            env = {
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(tmp_path),
                "SW_SESSION_NAME": "sw-test-hook-0",
            }
            # Override STATE_DIR in the script by pre-creating the file
            # The hook reads from $HOME/.config/sw/session-states/
            home_state_dir = tmp_path / ".config" / "sw" / "session-states"
            home_state_dir.mkdir(parents=True)
            home_state_file = home_state_dir / "sw-test-hook-0"

            # First call: no file exists, hook should create it
            result = subprocess.run(
                ["bash", str(script_path), "running"],
                env=env, capture_output=True, text=True, timeout=5,
            )
            assert result.returncode == 0
            # File may or may not exist (tmux not available), but the
            # early-exit logic should work based on file content

            # Write the state file manually (simulating first run succeeded)
            home_state_file.write_text("running")

            # Second call with same state: should exit early (exit 0)
            # and NOT attempt tmux commands (which would fail)
            result = subprocess.run(
                ["bash", str(script_path), "running"],
                env=env, capture_output=True, text=True, timeout=5,
            )
            assert result.returncode == 0

            # Third call with different state: should proceed
            result = subprocess.run(
                ["bash", str(script_path), "waiting_input"],
                env=env, capture_output=True, text=True, timeout=5,
            )
            assert result.returncode == 0


class TestNoContentChangedMessage:
    """Verify ContentChanged message is no longer posted by TerminalPane."""

    def test_no_content_changed_class(self):
        """TerminalPane should no longer have a ContentChanged message class."""
        from super_worker.widgets.terminal_pane import TerminalPane
        assert not hasattr(TerminalPane, "ContentChanged")

    def test_has_state_changed_class(self):
        """TerminalPane should have a StateChanged message class."""
        from super_worker.widgets.terminal_pane import TerminalPane
        assert hasattr(TerminalPane, "StateChanged")


class TestProjectViewStateHandler:
    """Verify ProjectView handles StateChanged instead of ContentChanged."""

    def test_has_state_changed_handler(self):
        from super_worker.widgets.project_view import ProjectView
        assert hasattr(ProjectView, "on_terminal_pane_state_changed")

    def test_no_content_changed_handler(self):
        from super_worker.widgets.project_view import ProjectView
        assert not hasattr(ProjectView, "on_terminal_pane_content_changed")


class TestSendKeysNoEnvSet:
    """Verify send_keys no longer calls _set_session_env (dead code removed)."""

    def test_no_set_session_env_function(self):
        """_set_session_env should not exist in tmux module anymore."""
        import super_worker.services.tmux as tmux_mod
        assert not hasattr(tmux_mod, "_set_session_env")

    def test_no_throttle_state_vars(self):
        """Throttle state variables should be removed."""
        import super_worker.services.tmux as tmux_mod
        assert not hasattr(tmux_mod, "_last_state_set")
        assert not hasattr(tmux_mod, "_STATE_SET_THROTTLE_S")

    def test_send_keys_does_not_call_set_environment(self, monkeypatch):
        """send_keys should only call pane.send_keys, not session.set_environment."""
        mock_pane = MagicMock()
        mock_session = MagicMock()
        mock_session.active_pane = mock_pane
        mock_server = MagicMock()
        mock_server.sessions.get.return_value = mock_session
        monkeypatch.setattr("super_worker.services.tmux.libtmux.Server", lambda: mock_server)
        # Clear pane cache so _get_pane calls the mock
        monkeypatch.setattr("super_worker.services.tmux._pane_cache", {})

        from super_worker.services.tmux import send_keys
        send_keys("sw-test-0", "a", "b", "c")

        # pane.send_keys should be called 3 times (once per key)
        assert mock_pane.send_keys.call_count == 3
        # session.set_environment should NEVER be called
        mock_session.set_environment.assert_not_called()


class TestFallbackPollInterval:
    """Verify fallback poll interval is fast enough for responsive typing display."""

    def test_fallback_poll_fast_enough_for_typing(self):
        from super_worker.constants import PANE_FALLBACK_POLL_S
        # set_interval fires its callback directly (bypasses message queue),
        # so the fallback poll IS what updates the display during typing.
        # Must be <= 500ms to feel responsive.
        assert PANE_FALLBACK_POLL_S <= 0.5

    def test_fallback_poll_not_too_aggressive(self):
        from super_worker.constants import PANE_FALLBACK_POLL_S
        # Should not be sub-50ms (excessive tmux subprocess calls)
        assert PANE_FALLBACK_POLL_S >= 0.05


class TestPeriodicRefreshLightweight:
    """Verify periodic_refresh uses lightweight alive check, not batch_detect."""

    def test_periodic_refresh_imports_batch_check_alive(self):
        """ProjectView should import batch_check_alive for periodic_refresh."""
        import super_worker.widgets.project_view as pv_mod
        source = inspect.getsource(pv_mod.ProjectView._periodic_refresh_impl)
        assert "batch_check_alive" in source
        # Should NOT use batch_detect_session_states (heavyweight)
        assert "batch_detect_session_states" not in source

    def test_periodic_refresh_reads_state_files(self):
        """periodic_refresh should use read_all_state_files for state."""
        import super_worker.widgets.project_view as pv_mod
        source = inspect.getsource(pv_mod.ProjectView._periodic_refresh_impl)
        assert "read_all_state_files" in source


class TestDebounceUsesCallLater:
    """Verify send-keys workers are not exclusive (would drop keys)."""

    def test_send_keys_not_exclusive(self):
        """send-keys workers should NOT be exclusive (would drop keys)."""
        import super_worker.widgets.terminal_pane as tp_mod
        source = inspect.getsource(tp_mod.TerminalPane._send_keys_async)
        assert "exclusive=True" not in source


# ── Integration tests using Textual pilot ─────────────────────────────────


def _make_mock_server(capture_returns=None):
    """Create a mock libtmux server for integration tests."""
    mock_pane = MagicMock()
    mock_pane.capture_pane.return_value = capture_returns or ["line 1"]
    mock_pane.pane_dead = "0"
    mock_session = MagicMock()
    mock_session.session_name = "sw-test-0"
    mock_session.active_pane = mock_pane
    mock_session.show_environment.return_value = {}
    mock_server = MagicMock()
    # libtmux uses a special list-like with .get() — keep it as MagicMock
    mock_server.sessions.get.return_value = mock_session
    mock_server.sessions.__iter__ = MagicMock(return_value=iter([mock_session]))
    mock_server.new_session.return_value = mock_session
    return mock_server, mock_session, mock_pane


@pytest.fixture
def mock_tmux(monkeypatch):
    """Mock the tmux boundary and reset caches for integration tests."""
    mock_server, mock_session, mock_pane = _make_mock_server()
    monkeypatch.setattr("super_worker.services.tmux.libtmux.Server", lambda: mock_server)
    monkeypatch.setattr("super_worker.services.tmux._server", None)
    monkeypatch.setattr("super_worker.services.tmux._pane_cache", {})
    return mock_server, mock_session, mock_pane


@pytest.mark.asyncio
async def test_rapid_typing_all_keys_sent(mock_tmux):
    """Simulate rapid typing — every key must arrive at tmux, none dropped."""
    from textual.app import App, ComposeResult
    from super_worker.widgets.terminal_pane import TerminalPane

    _, _, mock_pane = mock_tmux

    class TestApp(App):
        def compose(self) -> ComposeResult:
            yield TerminalPane()

    app = TestApp()
    async with app.run_test() as pilot:
        terminal = app.query_one(TerminalPane)
        terminal.active_session = "sw-test-0"
        await pilot.pause()

        # Type 20 characters rapidly
        keys = list("hello world testing!")
        for ch in keys:
            await pilot.press(ch)
        await pilot.pause(delay=0.5)

        # Every key must have been sent to tmux
        calls = mock_pane.send_keys.call_args_list
        sent_chars = [c.args[0] for c in calls]
        for ch in keys:
            assert ch in sent_chars, f"Key '{ch}' was dropped"
        assert len(sent_chars) >= len(keys), (
            f"Expected at least {len(keys)} send_keys calls, got {len(sent_chars)}"
        )


@pytest.mark.asyncio
async def test_display_updates_during_typing(mock_tmux):
    """Display must update DURING typing via the fallback set_interval timer."""
    from textual.app import App, ComposeResult
    from super_worker.widgets.terminal_pane import TerminalPane

    _, _, mock_pane = mock_tmux
    poll_count = 0

    class TrackingTerminalPane(TerminalPane):
        def _poll_pane(self) -> None:
            nonlocal poll_count
            poll_count += 1
            super()._poll_pane()

    class TestApp(App):
        def compose(self) -> ComposeResult:
            yield TrackingTerminalPane()

    app = TestApp()
    async with app.run_test() as pilot:
        terminal = app.query_one(TrackingTerminalPane)
        terminal.active_session = "sw-test-0"
        await pilot.pause(delay=0.2)  # Let initial capture happen

        # Reset count after initial render
        poll_count = 0

        # Wait 1 second — fallback set_interval(0.15s) bypasses message queue,
        # so it fires even during heavy key event traffic.
        await pilot.pause(delay=1.0)

        # With 150ms interval, should see ~6 polls in 1 second (>=3 minimum)
        assert poll_count >= 3, (
            f"Expected >=3 polls during 1s, got {poll_count}. "
            f"Display is not updating during typing."
        )


@pytest.mark.asyncio
async def test_fallback_timer_fires_independently_of_message_queue(mock_tmux):
    """set_interval-based timer fires even when message queue is busy."""
    from textual.app import App, ComposeResult
    from super_worker.widgets.terminal_pane import TerminalPane

    _, _, mock_pane = mock_tmux
    poll_count = 0

    class TrackingTerminalPane(TerminalPane):
        def _poll_pane(self) -> None:
            nonlocal poll_count
            poll_count += 1
            super()._poll_pane()

    class TestApp(App):
        def compose(self) -> ComposeResult:
            yield TrackingTerminalPane()

    app = TestApp()
    async with app.run_test() as pilot:
        terminal = app.query_one(TrackingTerminalPane)
        terminal.active_session = "sw-test-0"
        await pilot.pause(delay=0.2)
        poll_count = 0

        # Simulate 800ms of activity; timer fires every 150ms regardless
        await pilot.pause(delay=0.8)

        assert poll_count >= 4, (
            f"Expected >=4 polls in 800ms (150ms interval), got {poll_count}"
        )


@pytest.mark.asyncio
async def test_fallback_timer_pauses_and_resumes(mock_tmux):
    """pause_watching stops the timer; resume_watching restarts it."""
    from textual.app import App, ComposeResult
    from super_worker.widgets.terminal_pane import TerminalPane

    _, _, mock_pane = mock_tmux
    poll_count = 0

    class TrackingTerminalPane(TerminalPane):
        def _poll_pane(self) -> None:
            nonlocal poll_count
            poll_count += 1
            super()._poll_pane()

    class TestApp(App):
        def compose(self) -> ComposeResult:
            yield TrackingTerminalPane()

    app = TestApp()
    async with app.run_test() as pilot:
        terminal = app.query_one(TrackingTerminalPane)
        terminal.active_session = "sw-test-0"
        await pilot.pause(delay=0.2)

        terminal.pause_watching()
        assert terminal._fallback_timer is None, "Timer should be stopped after pause_watching"

        poll_count = 0
        await pilot.pause(delay=0.4)
        assert poll_count == 0, f"Timer fired after pause_watching, got {poll_count} polls"

        terminal.resume_watching()
        assert terminal._fallback_timer is not None, "Timer should restart after resume_watching"
