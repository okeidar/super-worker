from super_worker.models import AppState, Session, Worktree


def test_session_defaults_generate_unique_ids():
    s1 = Session(tmux_session_name="sw-a-0", label="first")
    s2 = Session(tmux_session_name="sw-a-1", label="second")
    assert s1.id != s2.id
    assert len(s1.id) == 8


def test_get_worktree_found():
    wt = Worktree(name="feat", path="/tmp/feat", branch="sw-feat")
    state = AppState(repo_root="/repo", worktree_base="/wt", worktrees=[wt])
    assert state.get_worktree("feat") is wt


def test_get_worktree_not_found():
    wt = Worktree(name="feat", path="/tmp/feat", branch="sw-feat")
    state = AppState(repo_root="/repo", worktree_base="/wt", worktrees=[wt])
    assert state.get_worktree("nonexistent") is None
