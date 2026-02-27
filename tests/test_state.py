import json
from pathlib import Path

import pytest

from super_worker.models import AppState, Session, Worktree
from super_worker.services.state import (
    _migrate_data,
    _state_file_for,
    load_projects_registry,
    load_state,
    reconcile_state,
    recover_dead_sessions,
    remove_session_from_state,
    remove_worktree_from_state,
    save_state,
    update_projects_registry,
)


@pytest.fixture()
def _redirect_state_dir(tmp_path, monkeypatch):
    """Redirect STATE_DIR to a temp directory for isolation."""
    state_dir = tmp_path / "sw-state"
    state_dir.mkdir()
    monkeypatch.setattr("super_worker.services.state.STATE_DIR", state_dir)
    return state_dir


class TestMigrateData:
    def test_renames_repo_path_to_repo_root(self):
        data = {"repo_path": "/old/path", "worktree_base": "/wt"}
        migrated = _migrate_data(data)
        assert migrated["repo_root"] == "/old/path"
        assert "repo_path" not in migrated

    def test_repo_root_takes_precedence_over_repo_path(self):
        data = {"repo_root": "/new", "repo_path": "/old", "worktree_base": "/wt"}
        migrated = _migrate_data(data)
        assert migrated["repo_root"] == "/new"


class TestLoadAndSaveState:
    @pytest.mark.usefixtures("_redirect_state_dir")
    def test_load_fresh_state_when_no_file(self, fake_config):
        state = load_state(fake_config)
        assert state.repo_root == str(fake_config.repo_root)
        assert state.worktrees == []

    @pytest.mark.usefixtures("_redirect_state_dir")
    def test_save_and_load_roundtrip(self, fake_config):
        wt = Worktree(name="feat", path="/tmp/feat", branch="sw-feat")
        session = Session(tmux_session_name="sw-feat-0", label="test")
        wt.sessions.append(session)

        state = AppState(
            repo_root=str(fake_config.repo_root),
            worktree_base=str(fake_config.base_dir),
            worktrees=[wt],
        )
        save_state(state, fake_config)

        loaded = load_state(fake_config)
        assert len(loaded.worktrees) == 1
        assert loaded.worktrees[0].name == "feat"
        assert loaded.worktrees[0].sessions[0].label == "test"

    @pytest.mark.usefixtures("_redirect_state_dir")
    def test_state_file_is_per_repo(self, fake_config):
        path = _state_file_for(fake_config)
        assert fake_config.state_hash in path.name


class TestRemoveFromState:
    def test_removes_matching_worktree(self):
        wt1 = Worktree(name="a", path="/a", branch="sw-a")
        wt2 = Worktree(name="b", path="/b", branch="sw-b")
        state = AppState(repo_root="/repo", worktree_base="/wt", worktrees=[wt1, wt2])
        result = remove_worktree_from_state(state, "a")
        assert [w.name for w in result.worktrees] == ["b"]

    def test_remove_worktree_no_op_for_missing(self):
        wt = Worktree(name="a", path="/a", branch="sw-a")
        state = AppState(repo_root="/repo", worktree_base="/wt", worktrees=[wt])
        result = remove_worktree_from_state(state, "nonexistent")
        assert len(result.worktrees) == 1

    def test_removes_matching_session(self):
        s1 = Session(id="aaa", tmux_session_name="sw-a-0", label="first")
        s2 = Session(id="bbb", tmux_session_name="sw-a-1", label="second")
        wt = Worktree(name="a", path="/a", branch="sw-a", sessions=[s1, s2])
        state = AppState(repo_root="/repo", worktree_base="/wt", worktrees=[wt])
        result = remove_session_from_state(state, "a", "aaa")
        assert [s.id for s in result.worktrees[0].sessions] == ["bbb"]

    def test_remove_session_no_op_for_missing_worktree(self):
        state = AppState(repo_root="/repo", worktree_base="/wt")
        result = remove_session_from_state(state, "nonexistent", "abc")
        assert result.worktrees == []


class TestReconcileState:
    def test_prunes_missing_paths(self, tmp_path, monkeypatch):
        existing_dir = tmp_path / "existing"
        existing_dir.mkdir()
        wt_existing = Worktree(name="exists", path=str(existing_dir), branch="sw-exists")
        wt_gone = Worktree(name="gone", path="/nonexistent/path", branch="sw-gone")
        state = AppState(
            repo_root=str(tmp_path),
            worktree_base=str(tmp_path),
            worktrees=[wt_existing, wt_gone],
        )
        monkeypatch.setattr("super_worker.services.state.prune_git_cache", lambda paths: None)
        changed = reconcile_state(state)
        assert changed is True
        assert [w.name for w in state.worktrees] == ["exists"]

    def test_no_change_when_all_exist(self, tmp_path, monkeypatch):
        for name in ("a", "b"):
            (tmp_path / name).mkdir()
        wt1 = Worktree(name="a", path=str(tmp_path / "a"), branch="sw-a")
        wt2 = Worktree(name="b", path=str(tmp_path / "b"), branch="sw-b")
        state = AppState(
            repo_root=str(tmp_path),
            worktree_base=str(tmp_path),
            worktrees=[wt1, wt2],
        )
        monkeypatch.setattr("super_worker.services.state.prune_git_cache", lambda paths: None)
        assert reconcile_state(state) is False
        assert len(state.worktrees) == 2


class TestProjectsRegistry:
    @pytest.mark.usefixtures("_redirect_state_dir")
    def test_update_load_and_dedup(self, fake_config):
        update_projects_registry(fake_config)
        update_projects_registry(fake_config)  # duplicate
        projects = load_projects_registry()
        assert projects.count(str(fake_config.repo_root)) == 1

    @pytest.mark.usefixtures("_redirect_state_dir")
    def test_load_empty_registry(self):
        assert load_projects_registry() == []

    @pytest.mark.usefixtures("_redirect_state_dir")
    def test_load_corrupted_registry(self, _redirect_state_dir):
        registry = _redirect_state_dir / "projects.json"
        registry.write_text("not valid json{{{")
        assert load_projects_registry() == []


class TestRecoverDeadSessions:
    def test_no_dead_sessions_is_noop(self, tmp_path, monkeypatch):
        wt_path = tmp_path / "feat"
        wt_path.mkdir()
        s = Session(tmux_session_name="sw-feat-0", label="alive")
        wt = Worktree(name="feat", path=str(wt_path), branch="sw-feat", sessions=[s])
        state = AppState(repo_root=str(tmp_path), worktree_base=str(tmp_path), worktrees=[wt])

        monkeypatch.setattr("super_worker.services.state.is_session_alive", lambda name: True)

        assert recover_dead_sessions(state) is False
        assert state.worktrees[0].sessions[0].tmux_session_name == "sw-feat-0"

    def test_dead_sessions_replaced_with_resumed(self, tmp_path, monkeypatch):
        wt_path = tmp_path / "feat"
        wt_path.mkdir()
        s = Session(tmux_session_name="sw-feat-0", label="dead-one")
        wt = Worktree(name="feat", path=str(wt_path), branch="sw-feat", sessions=[s])
        state = AppState(repo_root=str(tmp_path), worktree_base=str(tmp_path), worktrees=[wt])

        monkeypatch.setattr("super_worker.services.state.is_session_alive", lambda name: False)
        created_kwargs = []

        def fake_create(worktree, **kwargs):
            new_s = Session(tmux_session_name="sw-feat-1", label=kwargs.get("label", "new"))
            worktree.sessions.append(new_s)
            created_kwargs.append(kwargs)
            return new_s

        monkeypatch.setattr("super_worker.services.state.create_session", fake_create)

        assert recover_dead_sessions(state) is True
        assert state.worktrees[0].sessions[0].label == "(resumed)"
        assert created_kwargs[0]["resume"] is True

    def test_missing_worktree_path_skipped(self, monkeypatch):
        s = Session(tmux_session_name="sw-feat-0", label="dead")
        wt = Worktree(name="feat", path="/nonexistent/path", branch="sw-feat", sessions=[s])
        state = AppState(repo_root="/repo", worktree_base="/wt", worktrees=[wt])

        assert recover_dead_sessions(state) is False
        assert len(state.worktrees[0].sessions) == 1

    def test_alive_sessions_preserved_alongside_recovery(self, tmp_path, monkeypatch):
        wt_path = tmp_path / "feat"
        wt_path.mkdir()
        alive_s = Session(tmux_session_name="sw-feat-0", label="alive")
        dead_s = Session(tmux_session_name="sw-feat-1", label="dead")
        wt = Worktree(name="feat", path=str(wt_path), branch="sw-feat", sessions=[alive_s, dead_s])
        state = AppState(repo_root=str(tmp_path), worktree_base=str(tmp_path), worktrees=[wt])

        monkeypatch.setattr(
            "super_worker.services.state.is_session_alive",
            lambda name: name == "sw-feat-0",
        )

        def fake_create(worktree, **kwargs):
            new_s = Session(tmux_session_name="sw-feat-2", label=kwargs.get("label", "new"))
            worktree.sessions.append(new_s)
            return new_s

        monkeypatch.setattr("super_worker.services.state.create_session", fake_create)

        assert recover_dead_sessions(state) is True
        sessions = state.worktrees[0].sessions
        assert len(sessions) == 2
        assert sessions[0].tmux_session_name == "sw-feat-0"
        assert sessions[1].label == "(resumed)"
