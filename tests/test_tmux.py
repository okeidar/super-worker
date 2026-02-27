from unittest.mock import MagicMock

import pytest

from super_worker.models import Session, Worktree
from super_worker.services.tmux import (
    SessionState,
    _find_available_session_name,
    batch_detect_session_states,
    capture_pane,
    create_session,
    kill_session,
    kill_all_sessions,
    send_keys,
    tmux_session_name,
)


@pytest.mark.parametrize("name,index,expected", [
    ("my-feature", 0, "sw-my-feature-0"),
    ("feat", 3, "sw-feat-3"),
])
def test_tmux_session_name(name, index, expected):
    assert tmux_session_name(name, index) == expected


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
        ({}, SessionState.RUNNING),
        ({"SW_CC_STATE": "running"}, SessionState.RUNNING),
        ({"SW_CC_STATE": "waiting_input"}, SessionState.WAITING_INPUT),
        ({"SW_CC_STATE": "waiting_approval"}, SessionState.WAITING_APPROVAL),
        ({"SW_CC_STATE": "unknown_value"}, SessionState.RUNNING),
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

    def test_avoids_name_collision(self, monkeypatch):
        existing = MagicMock()
        existing.session_name = "sw-feat-0"
        self._mock_server(monkeypatch, existing_sessions=[existing])
        wt = Worktree(name="feat", path="/tmp/feat", branch="main")

        session = create_session(wt)

        assert session.tmux_session_name == "sw-feat-1"


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
