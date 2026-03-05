import hashlib
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

import git as gitpython
import pytest

from super_worker.config import (
    EnvConfig,
    GitConfig,
    ResolvedConfig,
    SWConfig,
    UIConfig,
    WorktreeConfig,
    _escape_toml_str,
    load_toml,
    _merge_configs,
    _toml_value,
    detect_main_branch,
    detect_remote,
    detect_repo_root,
    load_config,
    save_project_config,
)


@pytest.mark.parametrize("value,expected", [
    ("hello", '"hello"'),
    (True, "true"),
    (False, "false"),
    (42, "42"),
    (["a", "b"], '["a", "b"]'),
    ([], "[]"),
])
def test_toml_value(value, expected):
    assert _toml_value(value) == expected


@pytest.mark.parametrize("value,expected", [
    ("a\\b", "a\\\\b"),
    ('say "hi"', 'say \\"hi\\"'),
    ('a\\b "c"', 'a\\\\b \\"c\\"'),
    ("simple", "simple"),
])
def test_escape_toml_str(value, expected):
    assert _escape_toml_str(value) == expected


class TestDetectRepoRoot:
    def test_success(self, monkeypatch):
        mock_repo = MagicMock()
        mock_repo.working_dir = "/home/user/myrepo"
        mock_repo.git.rev_parse.return_value = ".git"  # normal repo: relative path
        monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)
        result = detect_repo_root()
        assert result == Path("/home/user/myrepo")

    def test_worktree_resolves_to_main_repo(self, monkeypatch, tmp_path):
        main_repo = tmp_path / "main"
        main_repo.mkdir()
        (main_repo / ".git").mkdir()
        worktree = tmp_path / "wt-branch"
        worktree.mkdir()

        mock_repo = MagicMock()
        mock_repo.working_dir = str(worktree)
        # Worktree: --git-common-dir returns absolute path to main .git
        mock_repo.git.rev_parse.return_value = str(main_repo / ".git")
        monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)
        result = detect_repo_root()
        assert result == main_repo

    def test_not_a_repo(self, monkeypatch):
        monkeypatch.setattr(
            gitpython, "Repo",
            MagicMock(side_effect=gitpython.InvalidGitRepositoryError("not a repo")),
        )
        with pytest.raises(RuntimeError, match="Not inside a git repository"):
            detect_repo_root()


class TestDetectRemote:
    def test_returns_origin_when_present(self, monkeypatch):
        mock_repo = MagicMock()
        upstream = MagicMock()
        upstream.name = "upstream"
        origin = MagicMock()
        origin.name = "origin"
        mock_repo.remotes = [upstream, origin]
        monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)
        assert detect_remote() == "origin"

    def test_returns_first_remote_when_no_origin(self, monkeypatch):
        mock_repo = MagicMock()
        upstream = MagicMock()
        upstream.name = "upstream"
        fork = MagicMock()
        fork.name = "fork"
        mock_repo.remotes = [upstream, fork]
        monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)
        assert detect_remote() == "upstream"

    def test_returns_origin_on_no_remotes(self, monkeypatch):
        mock_repo = MagicMock()
        mock_repo.remotes = []
        monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)
        assert detect_remote() == "origin"


class TestDetectMainBranch:
    def test_from_symbolic_ref(self, monkeypatch):
        mock_repo = MagicMock()
        mock_repo.git.symbolic_ref.return_value = "refs/remotes/origin/develop"
        monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)
        assert detect_main_branch("origin") == "develop"

    def test_fallback_to_main(self, monkeypatch):
        mock_repo = MagicMock()
        mock_repo.git.symbolic_ref.side_effect = gitpython.GitCommandError("symbolic-ref", 1)
        # rev_parse succeeds for "main"
        mock_repo.git.rev_parse.return_value = ""
        monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)
        assert detect_main_branch("origin") == "main"

    def test_fallback_to_master(self, monkeypatch):
        mock_repo = MagicMock()
        mock_repo.git.symbolic_ref.side_effect = gitpython.GitCommandError("symbolic-ref", 1)

        def fake_rev_parse(*args):
            ref = args[-1]
            if "main" in ref:
                raise gitpython.GitCommandError("rev-parse", 1)
            return ""

        mock_repo.git.rev_parse.side_effect = fake_rev_parse
        monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)
        assert detect_main_branch("origin") == "master"


class TestMergeConfigs:
    def test_project_overrides_global(self):
        project = SWConfig(worktree=WorktreeConfig(prefix="proj-prefix"))
        global_ = SWConfig(worktree=WorktreeConfig(prefix="global-prefix"))
        merged = _merge_configs(project, global_)
        assert merged.worktree.prefix == "proj-prefix"

    def test_global_fills_missing(self):
        project = SWConfig()
        global_ = SWConfig(git=GitConfig(remote="upstream"))
        merged = _merge_configs(project, global_)
        assert merged.git.remote == "upstream"

    def test_both_empty_gives_defaults(self):
        merged = _merge_configs(SWConfig(), SWConfig())
        assert merged.worktree.prefix == ""
        assert merged.git.main_branch == ""

    def test_multiple_sections(self):
        project = SWConfig(
            worktree=WorktreeConfig(prefix="my-proj"),
            env=EnvConfig(symlinks=[".env"]),
        )
        global_ = SWConfig(
            git=GitConfig(main_branch="develop"),
            ui=UIConfig(name_placeholder="my-feature"),
        )
        merged = _merge_configs(project, global_)
        assert merged.worktree.prefix == "my-proj"
        assert merged.env.symlinks == [".env"]
        assert merged.git.main_branch == "develop"
        assert merged.ui.name_placeholder == "my-feature"


class TestLoadToml:
    def test_missing_file_returns_default(self, tmp_path):
        result = load_toml(tmp_path / "nonexistent.toml")
        assert result == SWConfig()

    def test_valid_toml(self, tmp_path):
        toml_file = tmp_path / "config.toml"
        toml_file.write_text('[worktree]\nprefix = "my-proj"\n')
        result = load_toml(toml_file)
        assert result.worktree.prefix == "my-proj"


class TestSaveProjectConfig:
    def test_round_trip(self, tmp_path):
        config = SWConfig(
            worktree=WorktreeConfig(prefix="proj", branch_prefix="feat-"),
            env=EnvConfig(symlinks=[".venv", ".env"]),
        )
        path = save_project_config(tmp_path, config)
        assert path.exists()
        loaded = load_toml(path)
        assert loaded.worktree.prefix == "proj"
        assert loaded.worktree.branch_prefix == "feat-"
        assert loaded.env.symlinks == [".venv", ".env"]

    def test_empty_config_writes_minimal(self, tmp_path):
        path = save_project_config(tmp_path, SWConfig())
        content = path.read_text()
        # Default values are not written
        assert content.strip() == ""


class TestResolvedConfig:
    def test_state_hash_deterministic(self, tmp_path):
        cfg = ResolvedConfig(
            repo_root=tmp_path,
            worktree_prefix="p",
            branch_prefix="sw-",
            base_dir=tmp_path,
            symlinks=[],
            copies=[],
            post_create_hook="",
            main_branch="main",
            remote="origin",
            commit_placeholder="",
            name_placeholder="",
            branch_placeholder="",
        )
        h1 = cfg.state_hash
        h2 = cfg.state_hash
        assert h1 == h2
        assert len(h1) == 12

    def test_state_hash_differs_for_different_roots(self, tmp_path):
        def make_cfg(root):
            return ResolvedConfig(
                repo_root=root,
                worktree_prefix="p",
                branch_prefix="sw-",
                base_dir=tmp_path,
                symlinks=[],
                copies=[],
                post_create_hook="",
                main_branch="main",
                remote="origin",
                commit_placeholder="",
                name_placeholder="",
                branch_placeholder="",
            )

        c1 = make_cfg(tmp_path / "repo-a")
        c2 = make_cfg(tmp_path / "repo-b")
        assert c1.state_hash != c2.state_hash

    def test_state_hash_matches_sha256(self, tmp_path):
        cfg = ResolvedConfig(
            repo_root=tmp_path / "myrepo",
            worktree_prefix="p",
            branch_prefix="sw-",
            base_dir=tmp_path,
            symlinks=[],
            copies=[],
            post_create_hook="",
            main_branch="main",
            remote="origin",
            commit_placeholder="",
            name_placeholder="",
            branch_placeholder="",
        )
        expected = hashlib.sha256(str(tmp_path / "myrepo").encode()).hexdigest()[:12]
        assert cfg.state_hash == expected


class TestLoadConfig:
    def test_auto_detection(self, tmp_path, monkeypatch):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()

        mock_repo = MagicMock()
        mock_repo.working_dir = str(repo_root)
        origin = MagicMock()
        origin.name = "origin"
        mock_repo.remotes = [origin]
        mock_repo.git.symbolic_ref.return_value = "refs/remotes/origin/main"
        mock_repo.git.rev_parse.return_value = ".git"

        monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)
        cfg = load_config(str(repo_root))
        assert cfg.repo_root == repo_root
        assert cfg.main_branch == "main"
        assert cfg.remote == "origin"

    def test_project_toml_overrides(self, tmp_path, monkeypatch):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".sw.toml").write_text(
            '[worktree]\nprefix = "custom"\n[git]\nmain_branch = "develop"\n'
        )

        mock_repo = MagicMock()
        mock_repo.working_dir = str(repo_root)
        origin = MagicMock()
        origin.name = "origin"
        mock_repo.remotes = [origin]
        mock_repo.git.symbolic_ref.return_value = "refs/remotes/origin/main"
        mock_repo.git.rev_parse.return_value = ".git"

        monkeypatch.setattr(gitpython, "Repo", lambda *a, **kw: mock_repo)
        cfg = load_config(str(repo_root))
        assert cfg.worktree_prefix == "custom"
        assert cfg.main_branch == "develop"
