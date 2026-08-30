from unittest.mock import MagicMock

import pytest

from super_worker.models import Session, Worktree
from super_worker.services.tmux import (
    SessionState,
    _find_available_session_name,
    batch_check_alive,
    batch_detect_session_states,
    capture_pane,
    create_session,
    is_session_alive,
    kill_session,
    kill_all_sessions,
    respawn_pane,
    send_keys,
    tmux_session_name,
)


@pytest.mark.parametrize("name,index,expected", [
    ("my-feature", 0, "sw-my-feature-0"),
    ("feat", 3, "sw-feat-3"),
])
def test_tmux_session_name(name, index, expected):
    assert tmux_session_name(name, index) == expected


def test_default_session_label_uses_unique_index():
    """Default labels derive from the session's unique tmux index, not a count.

    Regression: the old ``f"session {len(worktree.sessions)}"`` repeated after a
    delete, producing two indistinguishable "session 1"s.
    """
    from super_worker.services.tmux import _default_session_label

    assert _default_session_label("sw-main-7350d8-1") == "session 1"
    assert _default_session_label("sw-main-7350d8-2") == "session 2"
    # Worktree names may contain hyphens — only the trailing index matters.
    assert _default_session_label("sw-aii-229-be7ab0-0") == "session 0"
    # Distinct names never collide on the label.
    assert _default_session_label("sw-x-1") != _default_session_label("sw-x-2")
    # Non-numeric tail falls back gracefully rather than crashing.
    assert _default_session_label("sw-weird") == "session"


def _mock_server(monkeypatch, session=None, pane=None):
    """Build a mock libtmux server with optional session and pane."""
    mock_pane = pane or MagicMock()
    mock_session = session or MagicMock()
    mock_session.active_pane = mock_pane
    mock_server = MagicMock()
    mock_server.sessions.get.return_value = mock_session
    monkeypatch.setattr("super_worker.services.tmux.libtmux.Server", lambda: mock_server)
    return mock_server, mock_session, mock_pane


def test_capture_pane_failure_returns_not_found(monkeypatch):
    """Dead session returns a 'not found' message instead of crashing."""
    mock_server = MagicMock()
    mock_server.sessions.get.side_effect = Exception("no session")
    monkeypatch.setattr("super_worker.services.tmux.libtmux.Server", lambda: mock_server)
    assert "not found" in capture_pane("sw-dead-0").lower()


def test_send_keys_dead_session_does_not_raise(monkeypatch):
    mock_server = MagicMock()
    mock_server.sessions.get.side_effect = Exception("no session")
    monkeypatch.setattr("super_worker.services.tmux.libtmux.Server", lambda: mock_server)
    send_keys("sw-dead-0", "Enter")  # Should not raise


class TestBatchDetectSessionStates:
    def _mock_alive(self, monkeypatch, sessions: list):
        mock_server = MagicMock()
        mock_server.sessions = sessions
        monkeypatch.setattr("super_worker.services.tmux.libtmux.Server", lambda: mock_server)

    def test_empty_input(self):
        assert batch_detect_session_states([]) == {}

    @pytest.mark.parametrize("env,expected_state", [
        ({}, SessionState.UNKNOWN),
        ({"SW_CC_STATE": "running"}, SessionState.RUNNING),
        ({"SW_CC_STATE": "waiting_input"}, SessionState.WAITING_INPUT),
        ({"SW_CC_STATE": "waiting_approval"}, SessionState.WAITING_APPROVAL),
        ({"SW_CC_STATE": "unknown_value"}, SessionState.UNKNOWN),
    ])
    def test_alive_session_state(self, monkeypatch, env, expected_state):
        alive_session = MagicMock()
        alive_session.session_name = "sw-a-0"
        alive_session.show_environment.return_value = env
        self._mock_alive(monkeypatch, [alive_session])

        result = batch_detect_session_states(["sw-a-0"])

        assert result["sw-a-0"] == expected_state


class TestCreateSession:
    def _mock_server(self, monkeypatch, existing_sessions=None):
        mock_tmux_session = MagicMock()
        mock_server = MagicMock()
        mock_server.sessions = existing_sessions or []
        mock_server.new_session.return_value = mock_tmux_session
        monkeypatch.setattr("super_worker.services.tmux.libtmux.Server", lambda: mock_server)
        return mock_server

    def test_creates_session_with_defaults(self, monkeypatch):
        server = self._mock_server(monkeypatch)
        wt = Worktree(name="feat", path="/tmp/feat", branch="main")

        session = create_session(wt)

        assert session.tmux_session_name.startswith("sw-feat-")
        assert session.label.startswith("session ")
        assert session.skip_permissions is False
        server.new_session.assert_called_once()

    def test_label_defaults_to_prompt(self, monkeypatch):
        self._mock_server(monkeypatch)
        wt = Worktree(name="feat", path="/tmp/feat", branch="main")

        session = create_session(wt, prompt="/plan")

        assert session.label == "/plan"
        assert session.initial_prompt == "/plan"

    def test_explicit_label_overrides_prompt(self, monkeypatch):
        self._mock_server(monkeypatch)
        wt = Worktree(name="feat", path="/tmp/feat", branch="main")

        session = create_session(wt, prompt="/plan", label="My Label")

        assert session.label == "My Label"

    def test_skip_permissions_flag(self, monkeypatch):
        server = self._mock_server(monkeypatch)
        wt = Worktree(name="feat", path="/tmp/feat", branch="main")

        session = create_session(wt, skip_permissions=True)

        assert session.skip_permissions is True
        cmd = server.new_session.call_args[1]["window_command"]
        assert "--dangerously-skip-permissions" in cmd

    def test_resume_flag(self, monkeypatch):
        server = self._mock_server(monkeypatch)
        wt = Worktree(name="feat", path="/tmp/feat", branch="main")

        create_session(wt, resume=True)

        cmd = server.new_session.call_args[1]["window_command"]
        assert "--continue" in cmd

    def test_new_claude_session_pins_session_id(self, monkeypatch):
        """A fresh claude session gets a stable --session-id stored on the model."""
        server = self._mock_server(monkeypatch)
        wt = Worktree(name="feat", path="/tmp/feat", branch="main")

        session = create_session(wt)

        assert session.claude_session_id is not None
        cmd = server.new_session.call_args[1]["window_command"]
        assert f"--session-id {session.claude_session_id}" in cmd

    def test_resume_specific_session_id(self, monkeypatch):
        """Resuming with a known id uses --resume <id>, not --continue."""
        server = self._mock_server(monkeypatch)
        wt = Worktree(name="feat", path="/tmp/feat", branch="main")

        session = create_session(wt, resume=True, resume_session_id="abc-123")

        cmd = server.new_session.call_args[1]["window_command"]
        assert "--resume abc-123" in cmd
        assert "--continue" not in cmd
        assert session.claude_session_id == "abc-123"

    def test_terminal_session_has_no_session_id(self, monkeypatch):
        self._mock_server(monkeypatch)
        wt = Worktree(name="feat", path="/tmp/feat", branch="main")

        session = create_session(wt, session_type="terminal")

        assert session.claude_session_id is None

    def test_creates_terminal_session(self, monkeypatch):
        server = self._mock_server(monkeypatch)
        monkeypatch.setenv("SHELL", "/bin/zsh")
        wt = Worktree(name="feat", path="/tmp/feat", branch="main")

        session = create_session(wt, session_type="terminal")

        assert session.session_type == "terminal"
        assert session.label == "terminal"
        cmd = server.new_session.call_args[1]["window_command"]
        assert "claude" not in cmd
        assert "/bin/zsh" in cmd

    def test_avoids_name_collision(self, monkeypatch):
        from super_worker.services.tmux import _worktree_scope, tmux_session_name
        wt = Worktree(name="feat", path="/tmp/feat", branch="main")
        scope = _worktree_scope(wt)
        existing = MagicMock()
        existing.session_name = tmux_session_name("feat", 0, scope)  # index 0 taken
        self._mock_server(monkeypatch, existing_sessions=[existing])

        session = create_session(wt)

        assert session.tmux_session_name == tmux_session_name("feat", 1, scope)

    def test_session_name_scoped_per_project(self, monkeypatch):
        """Same-named worktrees in different repos get distinct session names."""
        from super_worker.services.tmux import _worktree_scope
        wt_a = Worktree(name="main", path="/repo-a", branch="main")
        wt_b = Worktree(name="main", path="/repo-b", branch="main")
        assert _worktree_scope(wt_a) != _worktree_scope(wt_b)

        self._mock_server(monkeypatch)
        a = create_session(wt_a)
        self._mock_server(monkeypatch)
        b = create_session(wt_b)
        assert a.tmux_session_name != b.tmux_session_name


def test_kill_session_handles_missing(monkeypatch):
    mock_server = MagicMock()
    mock_server.sessions.get.side_effect = Exception("not found")
    monkeypatch.setattr("super_worker.services.tmux.libtmux.Server", lambda: mock_server)
    kill_session("sw-dead-0")  # Should not raise


def test_kill_all_sessions(monkeypatch):
    killed = []
    monkeypatch.setattr("super_worker.services.tmux.kill_session", lambda name: killed.append(name))
    wt = Worktree(name="feat", path="/tmp/feat", branch="main")
    wt.sessions = [
        Session(tmux_session_name="sw-feat-0", label="s0"),
        Session(tmux_session_name="sw-feat-1", label="s1"),
    ]

    kill_all_sessions(wt)

    assert killed == ["sw-feat-0", "sw-feat-1"]



class TestIsSessionAlive:
    def test_alive_session(self, monkeypatch):
        mock_pane = MagicMock()
        mock_pane.pane_dead = "0"
        _mock_server(monkeypatch, pane=mock_pane)
        assert is_session_alive("sw-feat-0") is True

    def test_dead_pane(self, monkeypatch):
        mock_pane = MagicMock()
        mock_pane.pane_dead = "1"
        _mock_server(monkeypatch, pane=mock_pane)
        assert is_session_alive("sw-feat-0") is False

    def test_missing_session(self, monkeypatch):
        mock_server = MagicMock()
        mock_server.sessions.get.side_effect = Exception("not found")
        monkeypatch.setattr("super_worker.services.tmux.libtmux.Server", lambda: mock_server)
        assert is_session_alive("sw-dead-0") is False


class TestBatchDetectDeadPanes:
    def test_dead_pane_returns_dead_state(self, monkeypatch):
        """A session with remain-on-exit and a dead pane returns DEAD."""
        alive_session = MagicMock()
        alive_session.session_name = "sw-a-0"
        dead_pane = MagicMock()
        dead_pane.pane_dead = "1"
        alive_session.active_pane = dead_pane

        mock_server = MagicMock()
        mock_server.sessions = [alive_session]
        monkeypatch.setattr("super_worker.services.tmux.libtmux.Server", lambda: mock_server)

        result = batch_detect_session_states(["sw-a-0"])
        assert result["sw-a-0"] == SessionState.DEAD


class TestBatchCheckAlive:
    """Tests for batch_check_alive — lightweight dead detection without show_environment."""

    def _mock_alive(self, monkeypatch, sessions: list):
        mock_server = MagicMock()
        mock_server.sessions = sessions
        monkeypatch.setattr("super_worker.services.tmux.libtmux.Server", lambda: mock_server)

    def test_empty_input(self):
        assert batch_check_alive([]) == set()

    def test_all_alive(self, monkeypatch):
        s1 = MagicMock()
        s1.session_name = "sw-a-0"
        s1.active_pane.pane_dead = "0"
        s2 = MagicMock()
        s2.session_name = "sw-b-0"
        s2.active_pane.pane_dead = "0"
        self._mock_alive(monkeypatch, [s1, s2])

        dead = batch_check_alive(["sw-a-0", "sw-b-0"])
        assert dead == set()

    def test_missing_session(self, monkeypatch):
        self._mock_alive(monkeypatch, [])

        dead = batch_check_alive(["sw-missing-0"])
        assert dead == {"sw-missing-0"}

    def test_dead_pane(self, monkeypatch):
        s1 = MagicMock()
        s1.session_name = "sw-a-0"
        dead_pane = MagicMock()
        dead_pane.pane_dead = "1"
        s1.active_pane = dead_pane
        self._mock_alive(monkeypatch, [s1])

        dead = batch_check_alive(["sw-a-0"])
        assert dead == {"sw-a-0"}

    def test_mixed_alive_dead_missing(self, monkeypatch):
        alive = MagicMock()
        alive.session_name = "sw-alive-0"
        alive.active_pane.pane_dead = "0"
        dead = MagicMock()
        dead.session_name = "sw-dead-0"
        dead_pane = MagicMock()
        dead_pane.pane_dead = "1"
        dead.active_pane = dead_pane
        self._mock_alive(monkeypatch, [alive, dead])

        result = batch_check_alive(["sw-alive-0", "sw-dead-0", "sw-missing-0"])
        assert result == {"sw-dead-0", "sw-missing-0"}

    def test_server_failure_returns_empty(self, monkeypatch):
        mock_server = MagicMock()
        mock_server.sessions.__iter__ = MagicMock(side_effect=Exception("tmux not running"))
        monkeypatch.setattr("super_worker.services.tmux.libtmux.Server", lambda: mock_server)

        dead = batch_check_alive(["sw-a-0"])
        assert dead == set()

    def test_does_not_call_show_environment(self, monkeypatch):
        """batch_check_alive should never call show_environment (that's the whole point)."""
        s1 = MagicMock()
        s1.session_name = "sw-a-0"
        s1.active_pane.pane_dead = "0"
        self._mock_alive(monkeypatch, [s1])

        batch_check_alive(["sw-a-0"])
        s1.show_environment.assert_not_called()


class TestRespawnPane:
    def test_respawn_calls_tmux_cmd(self, monkeypatch):
        mock_server = MagicMock()
        monkeypatch.setattr("super_worker.services.tmux.libtmux.Server", lambda: mock_server)

        result = respawn_pane("sw-feat-0", "claude --continue")

        assert result is True
        mock_server.cmd.assert_called_once_with(
            "respawn-pane", "-k", "-t", "sw-feat-0", "claude --continue"
        )

    def test_respawn_handles_failure(self, monkeypatch):
        mock_server = MagicMock()
        mock_server.cmd.side_effect = Exception("failed")
        monkeypatch.setattr("super_worker.services.tmux.libtmux.Server", lambda: mock_server)

        result = respawn_pane("sw-dead-0", "claude --continue")

        assert result is False


class TestCreateSessionRemainOnExit:
    def test_sets_remain_on_exit(self, monkeypatch):
        """create_session() enables remain-on-exit on the tmux session."""
        mock_tmux_session = MagicMock()
        mock_server = MagicMock()
        mock_server.sessions = []
        mock_server.new_session.return_value = mock_tmux_session
        monkeypatch.setattr("super_worker.services.tmux.libtmux.Server", lambda: mock_server)

        wt = Worktree(name="feat", path="/tmp/feat", branch="main")
        create_session(wt)

        mock_tmux_session.set_option.assert_any_call("remain-on-exit", "on")


class TestBracketedPaste:
    def test_paste_uses_bracketed_paste_buffer(self, monkeypatch, tmp_path):
        """paste_to_pane loads a buffer then paste-buffer -p (bracketed, if app wants it)."""
        from unittest.mock import MagicMock
        from super_worker.services.tmux import paste_to_pane

        server = MagicMock()
        monkeypatch.setattr("super_worker.services.tmux._get_server", lambda: server)

        paste_to_pane("sw-x-0", "multi\nline\ntext")

        calls = [c.args for c in server.cmd.call_args_list]
        assert any(c[0] == "load-buffer" for c in calls), "must load the text into a tmux buffer"
        paste = next((c for c in calls if c[0] == "paste-buffer"), None)
        assert paste is not None, "must paste-buffer"
        assert "-p" in paste, "must request bracketed paste (-p)"
        assert "sw-x-0" in paste, "must target the session"

    def test_paste_empty_is_noop(self, monkeypatch):
        from unittest.mock import MagicMock
        from super_worker.services.tmux import paste_to_pane
        server = MagicMock()
        monkeypatch.setattr("super_worker.services.tmux._get_server", lambda: server)
        paste_to_pane("sw-x-0", "")
        server.cmd.assert_not_called()
