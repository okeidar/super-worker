import time
from pathlib import Path
from unittest.mock import MagicMock

import git as gitpython
import pytest

from super_worker.config import ResolvedConfig
from super_worker.models import AppState, Worktree
from super_worker.services.worktree import (
    BranchExistsError,
    _GIT_CACHE_TTL,
    _branch_status_cache,
    _dirty_cache,
    create_worktree,
    discover_worktrees,
    get_branch_status,
    get_current_branch,
    get_worktree_dirty,
    invalidate_git_cache,
    prune_git_cache,
    remove_worktree,
)


@pytest.fixture(autouse=True)
def _clear_caches():
    """Clear module-level caches before each test."""
    _branch_status_cache.clear()
    _dirty_cache.clear()
    yield
    _branch_status_cache.clear()
    _dirty_cache.clear()


def test_branch_exists_error():
    err = BranchExistsError("sw-feat")
    assert err.branch == "sw-feat"
    assert "sw-feat" in str(err)


class TestGetBranchStatus:
    def test_parses_output(self, monkeypatch):
        mock_repo = MagicMock()
        mock_repo.git.rev_list.return_value = "3\t7"
        monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)
        status = get_branch_status("/tmp/wt")
        assert status == {"behind": 3, "ahead": 7}

    @pytest.mark.parametrize("side_effect", [
        gitpython.GitCommandError("rev-list", 1),
        None,  # malformed output case
    ])
    def test_returns_zeros_on_failure(self, monkeypatch, side_effect):
        if side_effect:
            monkeypatch.setattr(
                gitpython, "Repo",
                MagicMock(side_effect=side_effect),
            )
        else:
            mock_repo = MagicMock()
            mock_repo.git.rev_list.return_value = "garbage"
            monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)
        assert get_branch_status("/tmp/wt") == {"behind": 0, "ahead": 0}

    def test_uses_cache_and_expires(self, monkeypatch):
        call_count = 0

        def counting_repo(*a, **kw):
            nonlocal call_count
            call_count += 1
            mock = MagicMock()
            mock.git.rev_list.return_value = "1\t2"
            return mock

        monkeypatch.setattr(gitpython, "Repo", counting_repo)

        get_branch_status("/tmp/wt")
        get_branch_status("/tmp/wt")
        assert call_count == 1  # cached

        # Expire the cache entry
        ts, val = _branch_status_cache["/tmp/wt"]
        _branch_status_cache["/tmp/wt"] = (ts - _GIT_CACHE_TTL - 1, val)

        get_branch_status("/tmp/wt")
        assert call_count == 2  # re-fetched

    def test_uses_custom_remote_and_branch(self, monkeypatch):
        mock_repo = MagicMock()
        mock_repo.git.rev_list.return_value = "0\t0"
        monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)
        get_branch_status("/tmp/wt", remote="upstream", main_branch="develop")
        mock_repo.git.rev_list.assert_called_once_with(
            "--left-right", "--count", "upstream/develop...HEAD",
        )


@pytest.mark.parametrize("is_dirty", [True, False])
def test_get_worktree_dirty(monkeypatch, is_dirty):
    mock_repo = MagicMock()
    mock_repo.is_dirty.return_value = is_dirty
    monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)
    assert get_worktree_dirty("/tmp/wt") is is_dirty


def test_get_worktree_dirty_uses_cache(monkeypatch):
    call_count = 0

    def counting_repo(*a, **kw):
        nonlocal call_count
        call_count += 1
        mock = MagicMock()
        mock.is_dirty.return_value = True
        return mock

    monkeypatch.setattr(gitpython, "Repo", counting_repo)
    get_worktree_dirty("/tmp/wt")
    get_worktree_dirty("/tmp/wt")
    assert call_count == 1


def test_invalidate_git_cache(monkeypatch):
    mock_repo = MagicMock()
    mock_repo.git.rev_list.return_value = "1\t2"
    mock_repo.is_dirty.return_value = True
    monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)

    get_branch_status("/tmp/wt")
    get_worktree_dirty("/tmp/wt")
    assert "/tmp/wt" in _branch_status_cache and "/tmp/wt" in _dirty_cache

    invalidate_git_cache("/tmp/wt")
    assert "/tmp/wt" not in _branch_status_cache and "/tmp/wt" not in _dirty_cache

    # No error on missing key
    invalidate_git_cache("/nonexistent")


def test_prune_git_cache():
    _branch_status_cache["/a"] = (time.monotonic(), {"behind": 0, "ahead": 0})
    _branch_status_cache["/b"] = (time.monotonic(), {"behind": 0, "ahead": 0})
    _dirty_cache["/a"] = (time.monotonic(), False)
    _dirty_cache["/c"] = (time.monotonic(), True)

    prune_git_cache({"/a"})

    assert "/a" in _branch_status_cache and "/b" not in _branch_status_cache
    assert "/a" in _dirty_cache and "/c" not in _dirty_cache

    # Empty set clears all
    prune_git_cache(set())
    assert len(_branch_status_cache) == 0 and len(_dirty_cache) == 0


@pytest.mark.parametrize("success,expected", [
    (True, "feature-branch"),
    (False, "(unknown)"),
])
def test_get_current_branch(monkeypatch, success, expected):
    if success:
        mock_repo = MagicMock()
        mock_repo.active_branch.name = "feature-branch"
        monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)
    else:
        monkeypatch.setattr(
            gitpython, "Repo",
            MagicMock(side_effect=gitpython.InvalidGitRepositoryError("not a repo")),
        )
    assert get_current_branch("/tmp/repo") == expected


class TestCreateWorktree:
    def _make_config(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        base = tmp_path / "worktrees"
        base.mkdir()
        return ResolvedConfig(
            repo_root=repo,
            base_dir=base,
            worktree_prefix="sw",
            branch_prefix="sw-",
            main_branch="main",
            remote="origin",
            symlinks=[],
            copies=[],
            post_create_hook="",
            commit_placeholder="",
            name_placeholder="",
            branch_placeholder="",
        )

    def test_creates_worktree_with_new_branch(self, tmp_path, monkeypatch):
        config = self._make_config(tmp_path)
        mock_repo = MagicMock()
        mock_repo.git.rev_parse.side_effect = gitpython.GitCommandError("rev-parse", 1)
        monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)

        wt = create_worktree(config, "feat")

        assert wt.name == "feat"
        assert wt.branch == "sw-feat"
        mock_repo.git.worktree.assert_called_once()

    def test_raises_on_existing_path(self, tmp_path):
        config = self._make_config(tmp_path)
        (config.base_dir / "sw-feat").mkdir()
        with pytest.raises(FileExistsError):
            create_worktree(config, "feat")

    def test_raises_branch_exists_error(self, tmp_path, monkeypatch):
        config = self._make_config(tmp_path)
        mock_repo = MagicMock()
        mock_repo.git.rev_parse.return_value = ""
        monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)

        with pytest.raises(BranchExistsError) as exc_info:
            create_worktree(config, "feat")
        assert exc_info.value.branch == "sw-feat"

    def test_uses_existing_branch_when_allowed(self, tmp_path, monkeypatch):
        config = self._make_config(tmp_path)
        mock_repo = MagicMock()
        mock_repo.git.rev_parse.return_value = ""
        monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)

        wt = create_worktree(config, "feat", use_existing_branch=True)

        assert wt.branch == "sw-feat"
        assert "-b" not in mock_repo.git.worktree.call_args[0]

    def test_detach_mode(self, tmp_path, monkeypatch):
        config = self._make_config(tmp_path)
        mock_repo = MagicMock()
        monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)

        wt = create_worktree(config, "feat", detach=True)

        assert wt.branch == "(detached)"
        assert "--detach" in mock_repo.git.worktree.call_args[0]

    def test_raises_on_git_failure(self, tmp_path, monkeypatch):
        config = self._make_config(tmp_path)
        mock_repo = MagicMock()
        mock_repo.git.rev_parse.side_effect = gitpython.GitCommandError("rev-parse", 1)
        mock_repo.git.worktree.side_effect = gitpython.GitCommandError("worktree", 1, stderr="fatal: error")
        monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)

        with pytest.raises(RuntimeError, match="Failed to create worktree"):
            create_worktree(config, "feat")


class TestRemoveWorktree:
    def test_removes_successfully(self, tmp_path, monkeypatch):
        wt_path = tmp_path / "wt"
        wt_path.mkdir()
        state = AppState(
            repo_root=str(tmp_path),
            worktree_base=str(tmp_path),
            worktrees=[Worktree(name="feat", path=str(wt_path), branch="sw-feat")],
        )
        mock_repo = MagicMock()
        monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)

        remove_worktree(state, "feat")
        mock_repo.git.worktree.assert_any_call("remove", str(wt_path))

    def test_raises_for_unknown_worktree(self, tmp_path):
        state = AppState(repo_root=str(tmp_path), worktree_base=str(tmp_path))
        with pytest.raises(ValueError, match="not found"):
            remove_worktree(state, "nonexistent")

    def test_raises_on_dirty_without_force(self, tmp_path, monkeypatch):
        wt_path = tmp_path / "wt"
        wt_path.mkdir()
        state = AppState(
            repo_root=str(tmp_path),
            worktree_base=str(tmp_path),
            worktrees=[Worktree(name="feat", path=str(wt_path), branch="sw-feat")],
        )
        mock_repo = MagicMock()
        mock_repo.git.worktree.side_effect = gitpython.GitCommandError(
            "worktree", 1, stderr="contains modified or untracked files",
        )
        monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)

        with pytest.raises(RuntimeError, match="uncommitted changes"):
            remove_worktree(state, "feat")

    def test_force_flag_in_command(self, tmp_path, monkeypatch):
        wt_path = tmp_path / "wt"
        wt_path.mkdir()
        state = AppState(
            repo_root=str(tmp_path),
            worktree_base=str(tmp_path),
            worktrees=[Worktree(name="feat", path=str(wt_path), branch="sw-feat")],
        )
        mock_repo = MagicMock()
        monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)

        remove_worktree(state, "feat", force=True)
        mock_repo.git.worktree.assert_any_call("remove", "--force", str(wt_path))


class TestDiscoverWorktrees:
    def _make_config(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        base = tmp_path / "worktrees"
        base.mkdir()
        return ResolvedConfig(
            repo_root=repo,
            base_dir=base,
            worktree_prefix="sw",
            branch_prefix="sw-",
            main_branch="main",
            remote="origin",
            symlinks=[],
            copies=[],
            post_create_hook="",
            commit_placeholder="",
            name_placeholder="",
            branch_placeholder="",
        )

    def _porcelain(self, entries: list[tuple[str, str]]) -> str:
        """Build porcelain output from (path, branch_or_special) pairs."""
        blocks = []
        for path, ref in entries:
            if ref == "detached":
                blocks.append(f"worktree {path}\nHEAD abc123\ndetached\n")
            else:
                blocks.append(f"worktree {path}\nHEAD abc123\nbranch refs/heads/{ref}\n")
        return "\n".join(blocks)

    def test_discovers_matching_worktrees(self, tmp_path, monkeypatch):
        config = self._make_config(tmp_path)
        repo_root = str(config.repo_root)
        output = self._porcelain([
            (repo_root, "main"),
            (str(tmp_path / "worktrees" / "sw-alpha"), "sw-alpha"),
            (str(tmp_path / "worktrees" / "sw-beta"), "sw-beta"),
        ])
        mock_repo = MagicMock()
        mock_repo.git.worktree.return_value = output
        monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)

        result = discover_worktrees(config)

        assert {wt.name for wt in result} == {"alpha", "beta"}

    def test_skips_main_repo_and_non_matching_prefix(self, tmp_path, monkeypatch):
        config = self._make_config(tmp_path)
        repo_root = str(config.repo_root)
        output = self._porcelain([
            (repo_root, "main"),
            (str(tmp_path / "worktrees" / "other-feat"), "other-feat"),
            (str(tmp_path / "worktrees" / "sw-match"), "sw-match"),
        ])
        mock_repo = MagicMock()
        mock_repo.git.worktree.return_value = output
        monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)

        result = discover_worktrees(config)

        assert len(result) == 1
        assert result[0].name == "match"

    def test_handles_detached_head(self, tmp_path, monkeypatch):
        config = self._make_config(tmp_path)
        repo_root = str(config.repo_root)
        output = self._porcelain([
            (repo_root, "main"),
            (str(tmp_path / "worktrees" / "sw-detached"), "detached"),
        ])
        mock_repo = MagicMock()
        mock_repo.git.worktree.return_value = output
        monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)

        result = discover_worktrees(config)
        assert result[0].branch == "(detached)"

    def test_returns_empty_on_git_error(self, tmp_path, monkeypatch):
        config = self._make_config(tmp_path)
        monkeypatch.setattr(
            gitpython, "Repo",
            MagicMock(side_effect=gitpython.GitCommandError("worktree", 1)),
        )
        assert discover_worktrees(config) == []

    def test_handles_no_trailing_blank_line(self, tmp_path, monkeypatch):
        config = self._make_config(tmp_path)
        repo_root = str(config.repo_root)
        output = (
            f"worktree {repo_root}\nHEAD abc123\nbranch refs/heads/main\n\n"
            f"worktree {tmp_path / 'worktrees' / 'sw-last'}\nHEAD abc123\nbranch refs/heads/sw-last"
        )
        mock_repo = MagicMock()
        mock_repo.git.worktree.return_value = output
        monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)

        result = discover_worktrees(config)
        assert len(result) == 1
        assert result[0].name == "last"
