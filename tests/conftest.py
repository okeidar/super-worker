from pathlib import Path

import pytest

from super_worker.config import ResolvedConfig
import super_worker.services.tmux as _tmux_mod


@pytest.fixture(autouse=True)
def _reset_tmux_caches():
    """Reset tmux module-level caches between tests."""
    _tmux_mod._server = None
    _tmux_mod._pane_cache.clear()
    yield
    _tmux_mod._server = None
    _tmux_mod._pane_cache.clear()


@pytest.fixture()
def fake_config(tmp_path: Path) -> ResolvedConfig:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    base_dir = tmp_path / "worktrees"
    base_dir.mkdir()
    return ResolvedConfig(
        repo_root=repo_root,
        worktree_prefix="test-proj",
        branch_prefix="sw-",
        base_dir=base_dir,
        symlinks=[".venv"],
        copies=[],
        post_create_hook="",
        main_branch="main",
        remote="origin",
        commit_placeholder="Brief description",
        name_placeholder="feature-name",
        branch_placeholder="sw-<name>",
    )
