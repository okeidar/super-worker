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
    list_local_branches,
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

    def test_delete_branch_deletes_local_and_remote(self, tmp_path, monkeypatch):
        wt_path = tmp_path / "wt"
        wt_path.mkdir()
        state = AppState(
            repo_root=str(tmp_path),
            worktree_base=str(tmp_path),
            worktrees=[Worktree(name="feat", path=str(wt_path), branch="sw-feat")],
        )
        mock_repo = MagicMock()
        monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)

        remove_worktree(state, "feat", force=True, delete_branch=True, remote="origin")

        mock_repo.git.branch.assert_called_once_with("-D", "sw-feat")
        mock_repo.git.push.assert_called_once_with("origin", "--delete", "sw-feat")

    def test_delete_branch_without_remote_skips_push(self, tmp_path, monkeypatch):
        wt_path = tmp_path / "wt"
        wt_path.mkdir()
        state = AppState(
            repo_root=str(tmp_path),
            worktree_base=str(tmp_path),
            worktrees=[Worktree(name="feat", path=str(wt_path), branch="sw-feat")],
        )
        mock_repo = MagicMock()
        monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)

        remove_worktree(state, "feat", force=True, delete_branch=True)

        mock_repo.git.branch.assert_called_once_with("-D", "sw-feat")
        mock_repo.git.push.assert_not_called()

    def test_delete_branch_false_keeps_branch(self, tmp_path, monkeypatch):
        wt_path = tmp_path / "wt"
        wt_path.mkdir()
        state = AppState(
            repo_root=str(tmp_path),
            worktree_base=str(tmp_path),
            worktrees=[Worktree(name="feat", path=str(wt_path), branch="sw-feat")],
        )
        mock_repo = MagicMock()
        monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)

        remove_worktree(state, "feat", force=True, delete_branch=False, remote="origin")

        mock_repo.git.branch.assert_not_called()
        mock_repo.git.push.assert_not_called()

    def test_delete_branch_skips_detached(self, tmp_path, monkeypatch):
        wt_path = tmp_path / "wt"
        wt_path.mkdir()
        state = AppState(
            repo_root=str(tmp_path),
            worktree_base=str(tmp_path),
            worktrees=[Worktree(name="feat", path=str(wt_path), branch="(detached)")],
        )
        mock_repo = MagicMock()
        monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)

        remove_worktree(state, "feat", force=True, delete_branch=True, remote="origin")

        mock_repo.git.branch.assert_not_called()
        mock_repo.git.push.assert_not_called()

    def test_delete_branch_handles_git_errors(self, tmp_path, monkeypatch):
        wt_path = tmp_path / "wt"
        wt_path.mkdir()
        state = AppState(
            repo_root=str(tmp_path),
            worktree_base=str(tmp_path),
            worktrees=[Worktree(name="feat", path=str(wt_path), branch="sw-feat")],
        )
        mock_repo = MagicMock()
        mock_repo.git.branch.side_effect = gitpython.GitCommandError("branch", 1)
        mock_repo.git.push.side_effect = gitpython.GitCommandError("push", 1)
        monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)

        # Should not raise — errors are logged and swallowed
        remove_worktree(state, "feat", force=True, delete_branch=True, remote="origin")


class TestListLocalBranches:
    def test_returns_sorted_branch_names(self, monkeypatch):
        mock_repo = MagicMock()
        mock_head_main = MagicMock()
        mock_head_main.name = "main"
        mock_head_feat = MagicMock()
        mock_head_feat.name = "feat-xyz"
        mock_head_alpha = MagicMock()
        mock_head_alpha.name = "alpha"
        mock_repo.heads = [mock_head_main, mock_head_feat, mock_head_alpha]
        monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)

        result = list_local_branches(Path("/tmp/repo"))
        assert result == ["alpha", "feat-xyz", "main"]

    def test_returns_empty_on_error(self, monkeypatch):
        monkeypatch.setattr(
            gitpython, "Repo",
            MagicMock(side_effect=gitpython.InvalidGitRepositoryError("bad")),
        )
        assert list_local_branches(Path("/tmp/bad")) == []

    def test_returns_empty_for_no_branches(self, monkeypatch):
        mock_repo = MagicMock()
        mock_repo.heads = []
        monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)

        assert list_local_branches(Path("/tmp/repo")) == []


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


class TestGitOpCrashSafety:
    """Git helpers run in workers with exit_on_error=True — they must never raise."""

    def test_create_pr_gh_not_installed(self, monkeypatch):
        import subprocess
        from super_worker.services.worktree import git_create_pr

        def boom(args, **kw):
            if args and args[0] == "gh":
                raise FileNotFoundError("gh")
            raise AssertionError("unexpected call")
        monkeypatch.setattr(subprocess, "run", boom)

        ok, msg = git_create_pr("/tmp", "feature", open_browser=False)
        assert ok is False
        assert "gh" in msg.lower()

    def test_create_pr_gh_times_out(self, monkeypatch):
        import subprocess
        from super_worker.services.worktree import git_create_pr

        def slow(args, **kw):
            raise subprocess.TimeoutExpired(args, kw.get("timeout", 10))
        monkeypatch.setattr(subprocess, "run", slow)

        ok, msg = git_create_pr("/tmp", "feature", open_browser=False)
        assert ok is False and "timed out" in msg.lower()

    @pytest.mark.parametrize("fn_name,args", [
        ("git_push", ("/no/such/repo", "origin", "b")),
        ("git_pull", ("/no/such/repo", "origin", "main")),
        ("git_commit", ("/no/such/repo", "msg")),
    ])
    def test_git_ops_on_missing_repo_return_error(self, fn_name, args):
        import super_worker.services.worktree as w
        result = getattr(w, fn_name)(*args)   # must return a string, not raise
        assert isinstance(result, str) and result


class TestCommitStaging:
    """git_commit stages new files but never the sw-managed excludes."""

    def _init_repo(self, tmp_path):
        import subprocess
        repo = tmp_path / "wt"
        repo.mkdir()
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
               "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
        (repo / "tracked.txt").write_text("orig\n")
        subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True, env=env)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True, env=env)
        return repo, env

    def test_commit_includes_new_files_excludes_secrets(self, tmp_path):
        import subprocess
        from super_worker.services.worktree import git_commit

        repo, env = self._init_repo(tmp_path)
        (repo / "tracked.txt").write_text("changed\n")
        (repo / "newfile.py").write_text("print('hi')\n")  # NEW file
        (repo / ".env").write_text("SECRET=abc\n")          # copied secret

        err = git_commit(str(repo), "msg", exclude=[".env"])
        assert err is None

        committed = subprocess.run(
            ["git", "show", "--stat", "--name-only", "--format=", "HEAD"],
            cwd=repo, capture_output=True, text=True, env=env,
        ).stdout.split()
        assert "newfile.py" in committed, "new files must be committed (regression: git add -u dropped them)"
        assert "tracked.txt" in committed
        assert ".env" not in committed, "excluded secret must never be committed"
        assert (repo / ".env").exists(), "excluded file must remain on disk (unstaged, not deleted)"


class TestRemoveWorktreeAlreadyGone:
    """A worktree deleted outside Super Worker must still be removable (tab closes)."""

    def test_missing_path_does_not_raise_and_prunes(self, tmp_path, monkeypatch):
        gone = tmp_path / "already-deleted"   # never created → doesn't exist
        state = AppState(
            repo_root=str(tmp_path), worktree_base=str(tmp_path),
            worktrees=[Worktree(name="feat", path=str(gone), branch="sw-feat")],
        )
        mock_repo = MagicMock()
        monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)

        # must NOT raise even though the worktree is gone
        remove_worktree(state, "feat", force=True)

        # never tried to `git worktree remove` a nonexistent path...
        remove_calls = [c for c in mock_repo.git.worktree.call_args_list if c.args[:1] == ("remove",)]
        assert not remove_calls, "should skip git worktree remove when path is gone"
        # ...but did prune stale admin state
        mock_repo.git.worktree.assert_any_call("prune")

    def test_missing_path_with_already_deleted_branch_does_not_raise(self, tmp_path, monkeypatch):
        gone = tmp_path / "gone"
        state = AppState(
            repo_root=str(tmp_path), worktree_base=str(tmp_path),
            worktrees=[Worktree(name="feat", path=str(gone), branch="sw-feat")],
        )
        mock_repo = MagicMock()
        # branch already deleted locally + remotely
        mock_repo.git.branch.side_effect = gitpython.GitCommandError("branch", 1, stderr="not found")
        mock_repo.git.push.side_effect = gitpython.GitCommandError("push", 1, stderr="not found")
        monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)

        # branch deletion failures are best-effort — must not block tab removal
        remove_worktree(state, "feat", force=True, delete_branch=True, remote="origin")

    def test_present_but_unregistered_dir_not_deleted_but_no_raise(self, tmp_path, monkeypatch):
        # dir exists but isn't a linked worktree (.git is not a gitdir file)
        d = tmp_path / "stray"
        d.mkdir()
        state = AppState(
            repo_root=str(tmp_path), worktree_base=str(tmp_path),
            worktrees=[Worktree(name="feat", path=str(d), branch="sw-feat")],
        )
        mock_repo = MagicMock()
        mock_repo.git.worktree.side_effect = _worktree_side_effect_fail_remove()
        monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)

        remove_worktree(state, "feat", force=True)  # must not raise
        assert d.exists(), "must NOT delete a directory that isn't a linked worktree"


def _worktree_side_effect_fail_remove():
    """git.worktree: fail on 'remove', succeed on 'prune'."""
    def _se(*args, **kw):
        if args and args[0] == "remove":
            raise gitpython.GitCommandError("worktree", 1, stderr="fatal: is not a working tree")
        return ""
    return _se


class TestGitStatusOnMissingPath:
    """A worktree dir deleted outside sw must not crash status queries."""

    def test_branch_status_missing_path_returns_default(self):
        from super_worker.services.worktree import get_branch_status, invalidate_git_cache
        invalidate_git_cache("/nonexistent/gone-xyz")
        assert get_branch_status("/nonexistent/gone-xyz", "origin", "main") == {"behind": 0, "ahead": 0}

    def test_dirty_missing_path_returns_false(self):
        from super_worker.services.worktree import get_worktree_dirty, invalidate_git_cache
        invalidate_git_cache("/nonexistent/gone-xyz2")
        assert get_worktree_dirty("/nonexistent/gone-xyz2") is False
