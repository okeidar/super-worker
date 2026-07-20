import logging
import shutil
import subprocess
import threading
import time
import webbrowser
from pathlib import Path

import git as gitpython

from super_worker.config import ResolvedConfig
from super_worker.models import AppState, Worktree

logger = logging.getLogger(__name__)

# TTL cache for git operations (path -> (timestamp, value))
_GIT_CACHE_TTL = 5.0  # seconds
_cache_lock = threading.Lock()
_branch_status_cache: dict[str, tuple[float, dict]] = {}
_dirty_cache: dict[str, tuple[float, bool]] = {}


def list_local_branches(repo: Path) -> list[str]:
    """Return sorted list of local branch names."""
    try:
        git_repo = gitpython.Repo(repo)
        return sorted(h.name for h in git_repo.heads)
    except Exception:
        return []


def _branch_exists(repo: Path, branch: str) -> bool:
    """Check if a local branch exists."""
    try:
        git_repo = gitpython.Repo(repo)
        git_repo.git.rev_parse("--verify", f"refs/heads/{branch}")
        return True
    except gitpython.GitCommandError:
        return False


class BranchExistsError(Exception):
    """Raised when the target branch already exists and caller should decide."""

    def __init__(self, branch: str) -> None:
        self.branch = branch
        super().__init__(f"Branch '{branch}' already exists")


def create_worktree(
    config: ResolvedConfig,
    name: str,
    branch: str | None = None,
    use_existing_branch: bool = False,
    detach: bool = False,
    worktree_index: int = 0,
) -> Worktree:
    """Create a git worktree with environment setup.

    Raises BranchExistsError if branch exists and use_existing_branch is False.
    If detach is True, creates a detached HEAD worktree (no new branch).
    """
    repo = config.repo_root
    wt_path = config.base_dir / f"{config.worktree_prefix}-{name}"

    if wt_path.exists():
        raise FileExistsError(f"Worktree path already exists: {wt_path}")

    git_repo = gitpython.Repo(repo)

    if detach:
        target_branch = "(detached)"
        try:
            git_repo.git.worktree("add", "--detach", str(wt_path))
        except gitpython.GitCommandError as e:
            raise RuntimeError(f"Failed to create worktree: {e.stderr or e}") from e
    else:
        target_branch = branch or f"{config.branch_prefix}{name}"
        if _branch_exists(repo, target_branch):
            if not use_existing_branch:
                raise BranchExistsError(target_branch)
            try:
                git_repo.git.worktree("add", str(wt_path), target_branch)
            except gitpython.GitCommandError as e:
                raise RuntimeError(f"Failed to create worktree: {e.stderr or e}") from e
        else:
            try:
                git_repo.git.worktree("add", "-b", target_branch, str(wt_path), config.main_branch)
            except gitpython.GitCommandError as e:
                raise RuntimeError(f"Failed to create worktree: {e.stderr or e}") from e

    try:
        _setup_env(repo, wt_path, config)
        _run_post_create_hook(config.post_create_hook, wt_path, worktree_index)
    except Exception:
        try:
            git_repo.git.worktree("remove", "--force", str(wt_path))
        except gitpython.GitCommandError:
            pass
        raise

    return Worktree(name=name, path=str(wt_path), branch=target_branch)


def _setup_env(repo: Path, wt_path: Path, config: ResolvedConfig) -> None:
    """Symlink and copy paths from repo to worktree, exclude symlinks from git."""
    created_symlinks = []
    for link_name in config.symlinks:
        src = repo / link_name
        dst = wt_path / link_name
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)  # support nested paths (e.g. "a/b")
            dst.symlink_to(src)
            created_symlinks.append(link_name)

    # Exclude BOTH symlinks and copies from git so `git add -A` (used by the
    # commit action) never stages a symlinked venv or a copied .env secret.
    _add_git_excludes(wt_path, list(config.symlinks) + list(config.copies))

    for copy_name in config.copies:
        src = repo / copy_name
        dst = wt_path / copy_name
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))


def _run_post_create_hook(hook: str, wt_path: Path, index: int) -> None:
    """Run post-create hook script if configured."""
    if not hook:
        return
    # Compare resolved-to-resolved: on macOS the worktree base often sits
    # under a symlinked component (/tmp → /private/tmp), and comparing a
    # resolved hook path against an unresolved root silently skipped the hook.
    hook_path = (wt_path / hook).resolve()
    if not hook_path.is_relative_to(wt_path.resolve()):
        logger.warning("Post-create hook escapes worktree directory", extra={"hook": hook, "resolved": str(hook_path)})
        return
    if not hook_path.exists():
        logger.warning("Post-create hook not found", extra={"hook": hook, "path": str(hook_path)})
        return
    result = subprocess.run(
        [str(hook_path), str(wt_path), str(index)],
        cwd=wt_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        logger.warning(
            "Post-create hook failed",
            extra={"hook": hook, "returncode": result.returncode, "stderr": result.stderr},
        )


def _add_git_excludes(wt_path: Path, names: list[str]) -> None:
    """Add entries to the shared git exclude file (works for worktrees)."""
    try:
        git_repo = gitpython.Repo(wt_path)
        common_dir = git_repo.git.rev_parse("--git-common-dir")
    except gitpython.GitCommandError:
        return

    git_common = Path(common_dir)
    if not git_common.is_absolute():
        git_common = (wt_path / git_common).resolve()

    exclude_dir = git_common / "info"
    exclude_dir.mkdir(parents=True, exist_ok=True)
    exclude_file = exclude_dir / "exclude"

    existing = set()
    if exclude_file.exists():
        existing = set(exclude_file.read_text().splitlines())

    new_entries = [n for n in names if n not in existing]
    if new_entries:
        with open(exclude_file, "a") as f:
            for entry in new_entries:
                f.write(f"{entry}\n")


def remove_worktree(
    state: AppState,
    name: str,
    force: bool = False,
    delete_branch: bool = False,
    remote: str = "",
) -> None:
    """Remove a git worktree and its directory, optionally deleting the branch (local + remote)."""
    wt = state.get_worktree(name)
    if not wt:
        raise ValueError(f"Worktree not found: {name}")

    repo = Path(state.repo_root)
    wt_path = Path(wt.path)
    git_repo = gitpython.Repo(repo)
    branch = wt.branch

    # Never operate on the main working tree. The default "main" worktree's
    # path IS the repo root; git refuses to `worktree remove` it, and with
    # force=True the manual-cleanup fallback below would rmtree the whole repo.
    try:
        same_as_repo = wt_path.resolve() == repo.resolve()
    except OSError:
        same_as_repo = wt_path == repo
    if same_as_repo:
        raise RuntimeError(
            f"Refusing to remove '{name}': it is the main repository working tree, "
            "not a linked worktree."
        )

    # If the worktree is already gone from disk (deleted outside Super Worker —
    # e.g. after merging and running `git worktree remove`), there is nothing
    # to remove: skip straight to pruning stale admin state + branch cleanup so
    # the caller can still drop it from state and close the tab. Never raise
    # here — a stale tab you can't close is worse than a no-op.
    if wt_path.exists():
        try:
            args = ["remove"]
            if force:
                args.append("--force")
            args.append(str(wt_path))
            git_repo.git.worktree(*args)
        except gitpython.GitCommandError as e:
            stderr = str(e.stderr or e)
            if not force and "contains modified or untracked files" in stderr:
                raise RuntimeError(
                    f"Worktree has uncommitted changes. Use --force to remove anyway.\n{stderr}"
                ) from e
            if not force:
                raise RuntimeError(f"Failed to remove worktree: {stderr}") from e
            # force=True: git couldn't remove it (e.g. no longer registered as a
            # worktree). Delete the directory ONLY if it's a genuine linked
            # worktree (its `.git` is a gitdir-pointer file, never a real repo).
            # If it's some other directory, leave it on disk but still fall
            # through — we don't want an un-closable tab over a stray folder.
            if (wt_path / ".git").is_file():
                shutil.rmtree(wt_path, ignore_errors=True)
            else:
                logger.warning(
                    "git could not remove worktree '%s' and it is not a linked "
                    "worktree dir; leaving the directory in place: %s", wt_path, stderr,
                )

    # Prune stale worktree admin entries — this is what cleans up a worktree
    # that was deleted outside Super Worker.
    try:
        git_repo.git.worktree("prune")
    except gitpython.GitCommandError:
        pass

    if delete_branch and branch and branch != "(detached)":
        try:
            git_repo.git.branch("-D", branch)
        except gitpython.GitCommandError:
            logger.debug("Failed to delete local branch %s", branch)
        # Also delete the remote branch
        if remote:
            try:
                git_repo.git.push(remote, "--delete", branch)
            except gitpython.GitCommandError:
                logger.debug("Failed to delete remote branch %s/%s", remote, branch)


def get_current_branch(repo_path: str) -> str:
    """Get the current branch name of a git repo or worktree."""
    try:
        repo = gitpython.Repo(repo_path)
        return repo.active_branch.name
    except (gitpython.InvalidGitRepositoryError, TypeError):
        return "(unknown)"


def get_branch_status(wt_path: str, remote: str = "origin", main_branch: str = "main") -> dict:
    """Get ahead/behind counts relative to remote/main_branch. Cached with TTL."""
    now = time.monotonic()
    with _cache_lock:
        cached = _branch_status_cache.get(wt_path)
        if cached and (now - cached[0]) < _GIT_CACHE_TTL:
            return cached[1]

    try:
        repo = gitpython.Repo(wt_path)
        output = repo.git.rev_list("--left-right", "--count", f"{remote}/{main_branch}...HEAD")
        parts = output.strip().split("\t")
        value = {"behind": int(parts[0]), "ahead": int(parts[1])}
    except (gitpython.NoSuchPathError, gitpython.GitCommandError,
            gitpython.InvalidGitRepositoryError, IndexError, ValueError):
        # NoSuchPathError: the worktree dir is gone (deleted outside sw) — a
        # stale tab must still render, not crash the sidebar/periodic refresh.
        value = {"behind": 0, "ahead": 0}
    with _cache_lock:
        _branch_status_cache[wt_path] = (now, value)
    return value


def get_worktree_dirty(wt_path: str) -> bool:
    """Check if worktree has uncommitted changes. Cached with TTL."""
    now = time.monotonic()
    with _cache_lock:
        cached = _dirty_cache.get(wt_path)
        if cached and (now - cached[0]) < _GIT_CACHE_TTL:
            return cached[1]

    try:
        repo = gitpython.Repo(wt_path)
        value = repo.is_dirty(untracked_files=True)
    except (gitpython.NoSuchPathError, gitpython.InvalidGitRepositoryError, gitpython.GitCommandError):
        value = False
    with _cache_lock:
        _dirty_cache[wt_path] = (now, value)
    return value


def invalidate_git_cache(wt_path: str) -> None:
    """Invalidate git caches for a worktree path after git operations."""
    with _cache_lock:
        _branch_status_cache.pop(wt_path, None)
        _dirty_cache.pop(wt_path, None)


# ---------------------------------------------------------------------------
# Shared git operations (used by both TUI and fast mode)
# ---------------------------------------------------------------------------


# Repo-open failures (worktree dir deleted out from under us, or corrupted)
# must be caught alongside command failures — these run in workers with
# exit_on_error=True, so an uncaught exception crashes the whole app.
_GIT_ERRORS = (
    gitpython.GitCommandError,
    gitpython.InvalidGitRepositoryError,
    gitpython.NoSuchPathError,
)


def git_push(wt_path: str, remote: str, branch: str) -> str | None:
    """Push branch to remote. Returns error message or None on success."""
    try:
        repo = gitpython.Repo(wt_path)
        repo.git.push("-u", remote, branch)
        invalidate_git_cache(wt_path)
        return None
    except _GIT_ERRORS as e:
        return str(getattr(e, "stderr", None) or e)[:200]


def git_pull(wt_path: str, remote: str, main_branch: str) -> str | None:
    """Pull main_branch from remote. Returns error message or None on success."""
    try:
        repo = gitpython.Repo(wt_path)
        repo.git.pull(remote, main_branch)
        invalidate_git_cache(wt_path)
        return None
    except _GIT_ERRORS as e:
        return str(getattr(e, "stderr", None) or e)[:200]


def git_commit(wt_path: str, message: str, exclude: list[str] | None = None) -> str | None:
    """Stage all changes and commit. Returns error message or None on success.

    Uses ``git add -A`` so files Claude *created* are committed too (``-u``
    silently dropped every new file). ``exclude`` names — the sw-managed
    symlinks/copies like ``.venv`` or ``.env`` — are unstaged before commit so
    a copied secret is never committed, even in worktrees created before these
    paths were added to git's exclude file.
    """
    try:
        repo = gitpython.Repo(wt_path)
        repo.git.add("-A")
        for name in exclude or []:
            try:
                repo.git.reset("-q", "--", name)
            except gitpython.GitCommandError:
                pass  # not staged / doesn't exist — nothing to unstage
        repo.git.commit("-m", message)
        invalidate_git_cache(wt_path)
        return None
    except _GIT_ERRORS as e:
        return str(getattr(e, "stderr", None) or e)[:200]


def git_create_pr(wt_path: str, branch: str, open_browser: bool = True) -> tuple[bool, str]:
    """Create a GitHub PR via gh CLI. Returns (success, url_or_error).

    If open_browser is True and PR creation succeeds, opens the URL in a browser.
    """
    # gh is optional (README) — a missing binary raises FileNotFoundError,
    # and a hung gh could raise TimeoutExpired; both must be handled or the
    # PR button crashes the app.
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        return False, "GitHub CLI (gh) is not installed. See https://cli.github.com/"
    except subprocess.TimeoutExpired:
        return False, "gh auth status timed out."
    if result.returncode != 0:
        return False, "gh CLI not authenticated. Run: gh auth login"

    try:
        result = subprocess.run(
            ["gh", "pr", "create", "--fill", "--head", branch],
            cwd=wt_path, capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError:
        return False, "GitHub CLI (gh) is not installed. See https://cli.github.com/"
    except subprocess.TimeoutExpired:
        return False, "gh pr create timed out."
    if result.returncode == 0:
        url = result.stdout.strip()
        if open_browser and url:
            try:
                webbrowser.open(url)
            except Exception:
                logger.debug("Failed to open PR URL in browser", exc_info=True)
        return True, url
    return False, (result.stderr or "")[:200]


def discover_worktrees(config: ResolvedConfig) -> list[Worktree]:
    """Discover git worktrees from .git that match the configured prefix.

    Uses `git worktree list` as the source of truth. Returns Worktree objects
    for sw-managed worktrees (matching the configured prefix) that aren't in state.
    The main repo checkout is excluded.
    """
    repo_root = str(config.repo_root)
    prefix = f"{config.worktree_prefix}-"
    try:
        repo = gitpython.Repo(repo_root)
        output = repo.git.worktree("list", "--porcelain")
    except (gitpython.GitCommandError, gitpython.InvalidGitRepositoryError, gitpython.NoSuchPathError):
        return []

    discovered: list[Worktree] = []
    current_path = ""
    current_branch = ""
    for line in output.splitlines():
        if line.startswith("worktree "):
            current_path = line[len("worktree "):]
            current_branch = ""
        elif line.startswith("branch "):
            current_branch = line[len("branch refs/heads/"):]
        elif line == "detached":
            current_branch = "(detached)"
        elif line == "" and current_path:
            _process_worktree_entry(current_path, current_branch, repo_root, prefix, discovered)
            current_path = ""

    # Handle last entry if output doesn't end with blank line
    if current_path:
        _process_worktree_entry(current_path, current_branch, repo_root, prefix, discovered)

    return discovered


def _process_worktree_entry(
    path: str, branch: str, repo_root: str, prefix: str, out: list[Worktree],
) -> None:
    if path == repo_root:
        return
    wt_dir = Path(path)
    if not wt_dir.name.startswith(prefix):
        return
    name = wt_dir.name[len(prefix):]
    # A manually created dir like "repo-fix.stuff" would yield a name that
    # crashes Textual at compose time (widget id "wt-fix.stuff" is invalid).
    from super_worker.constants import is_valid_worktree_name
    if not is_valid_worktree_name(name):
        logger.info("Skipping discovered worktree with unusable name: %s", wt_dir.name)
        return
    out.append(Worktree(name=name, path=path, branch=branch or "(unknown)"))


def prune_git_cache(valid_paths: set[str]) -> None:
    """Remove cache entries for worktree paths that no longer exist."""
    with _cache_lock:
        for stale in set(_branch_status_cache) - valid_paths:
            del _branch_status_cache[stale]
        for stale in set(_dirty_cache) - valid_paths:
            del _dirty_cache[stale]
