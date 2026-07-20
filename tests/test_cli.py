import pytest
from click.testing import CliRunner

from super_worker.cli import cli
from super_worker.config import ResolvedConfig
from super_worker.models import AppState, Session, Worktree


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def mock_env(tmp_path, monkeypatch):
    """Mock config loading and state to avoid real git/tmux."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    base_dir = tmp_path / "worktrees"
    base_dir.mkdir()

    config = ResolvedConfig(
        repo_root=repo_root,
        worktree_prefix="sw",
        branch_prefix="sw-",
        base_dir=base_dir,
        symlinks=[],
        copies=[],
        post_create_hook="",
        main_branch="main",
        remote="origin",
        commit_placeholder="",
        name_placeholder="",
        branch_placeholder="",
    )

    wt = Worktree(name="main", path=str(repo_root), branch="main")
    wt.sessions.append(Session(tmux_session_name="sw-main-0", label="session 0"))
    state = AppState(repo_root=str(repo_root), worktree_base=str(base_dir), worktrees=[wt])

    from contextlib import contextmanager

    @contextmanager
    def _fake_mutate(*a, **kw):
        # Mirror the real mutate_state: yield the in-memory state under "lock".
        yield state

    monkeypatch.setattr("super_worker.cli.load_config", lambda *a, **kw: config)
    monkeypatch.setattr("super_worker.cli.load_state", lambda *a, **kw: state)
    monkeypatch.setattr("super_worker.cli.mutate_state", _fake_mutate)
    monkeypatch.setattr("super_worker.cli.update_projects_registry", lambda *a, **kw: None)
    monkeypatch.setattr("super_worker.cli.save_state", lambda *a, **kw: None)

    return config, state


def test_list_shows_worktrees(runner, mock_env, monkeypatch):
    monkeypatch.setattr("super_worker.cli.is_session_alive", lambda *a, **kw: True)
    monkeypatch.setattr("super_worker.cli.get_branch_status", lambda *a, **kw: {"ahead": 1, "behind": 0})
    monkeypatch.setattr("super_worker.cli.get_worktree_dirty", lambda *a, **kw: False)

    result = runner.invoke(cli, ["list"])

    assert result.exit_code == 0
    assert "main" in result.output
    assert "sw-main-0" in result.output
    assert "session 0" in result.output
    assert "alive" in result.output
    assert "↑1 ↓0" in result.output


def test_list_empty(runner, mock_env, monkeypatch):
    _, state = mock_env
    state.worktrees.clear()

    result = runner.invoke(cli, ["list"])

    assert result.exit_code == 0
    assert "No worktrees." in result.output


def test_new_creates_worktree(runner, mock_env, monkeypatch):
    _, state = mock_env
    created = Worktree(name="feat", path="/tmp/feat", branch="sw-feat")
    monkeypatch.setattr("super_worker.cli.create_worktree", lambda *a, **kw: created)

    result = runner.invoke(cli, ["new", "feat"])

    assert result.exit_code == 0
    assert "feat" in result.output
    assert "sw-feat" in result.output


def test_new_duplicate_name_fails(runner, mock_env):
    result = runner.invoke(cli, ["new", "main"])

    assert result.exit_code != 0
    assert "already exists" in result.output


def test_add_session_to_worktree(runner, mock_env, monkeypatch):
    new_session = Session(tmux_session_name="sw-main-1", label="/plan")
    monkeypatch.setattr("super_worker.cli.create_session", lambda *a, **kw: new_session)

    result = runner.invoke(cli, ["add", "main", "--prompt", "/plan"])

    assert result.exit_code == 0
    assert "sw-main-1" in result.output
    assert "/plan" in result.output


def test_add_session_unknown_worktree_fails(runner, mock_env):
    result = runner.invoke(cli, ["add", "nonexistent"])

    assert result.exit_code != 0
    assert "not found" in result.output


def test_cleanup_kills_and_removes(runner, mock_env, monkeypatch):
    _, state = mock_env
    feat_wt = Worktree(name="feat", path="/tmp/feat", branch="sw-feat")
    feat_wt.sessions.append(Session(tmux_session_name="sw-feat-0", label="session 0"))
    state.worktrees.append(feat_wt)

    monkeypatch.setattr("super_worker.cli.kill_all_sessions", lambda *a, **kw: None)
    monkeypatch.setattr("super_worker.cli.remove_worktree", lambda *a, **kw: None)
    monkeypatch.setattr("super_worker.cli.remove_worktree_from_state", lambda s, *a, **kw: s)

    result = runner.invoke(cli, ["cleanup", "feat"])

    assert result.exit_code == 0
    assert "Killed" in result.output
    assert "Removed" in result.output


def test_config_show(runner, mock_env):
    config, _ = mock_env

    result = runner.invoke(cli, ["config"])

    assert result.exit_code == 0
    assert str(config.repo_root) in result.output
    assert config.branch_prefix in result.output
    assert config.main_branch in result.output
    assert config.remote in result.output
