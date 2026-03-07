import fcntl
import json
import logging
from pathlib import Path

from super_worker.config import ResolvedConfig
from super_worker.constants import STATE_DIR
from super_worker.models import AppState
from super_worker.services.tmux import create_session, is_session_alive
from super_worker.services.worktree import discover_worktrees, prune_git_cache

logger = logging.getLogger(__name__)


def _ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def _state_file_for(config: ResolvedConfig) -> Path:
    """Per-repo state file keyed by repo root path hash."""
    return STATE_DIR / f"state-{config.state_hash}.json"


def _migrate_data(data: dict) -> dict:
    """Handle backward-compatible field renames."""
    if "repo_path" in data and "repo_root" not in data:
        data["repo_root"] = data.pop("repo_path")
    return data


def load_state(config: ResolvedConfig) -> AppState:
    _ensure_state_dir()
    state_file = _state_file_for(config)

    # Try legacy state.json if per-repo file doesn't exist,
    # but only if it belongs to this repo
    legacy_file = STATE_DIR / "state.json"
    if not state_file.exists() and legacy_file.exists():
        try:
            legacy_data = json.loads(legacy_file.read_text())
            legacy_root = legacy_data.get("repo_root") or legacy_data.get("repo_path", "")
            if str(config.repo_root) == legacy_root:
                state_file = legacy_file
        except (json.JSONDecodeError, OSError):
            logger.debug("Failed to read legacy state file, starting fresh")

    if not state_file.exists():
        return AppState(
            repo_root=str(config.repo_root),
            worktree_base=str(config.base_dir),
        )
    lock_file = state_file.with_suffix(".lock")
    with open(lock_file, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_SH)
        try:
            data = json.loads(state_file.read_text())
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)

    data = _migrate_data(data)
    return AppState.model_validate(data)


def save_state(state: AppState, config: ResolvedConfig) -> None:
    _ensure_state_dir()
    state_file = _state_file_for(config)
    lock_file = state_file.with_suffix(".lock")
    tmp = state_file.with_suffix(".tmp")
    with open(lock_file, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            tmp.write_text(state.model_dump_json(indent=2))
            tmp.rename(state_file)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def remove_worktree_from_state(state: AppState, name: str) -> AppState:
    state.worktrees = [wt for wt in state.worktrees if wt.name != name]
    return state


def remove_session_from_state(state: AppState, worktree_name: str, session_id: str) -> AppState:
    wt = state.get_worktree(worktree_name)
    if wt:
        wt.sessions = [s for s in wt.sessions if s.id != session_id]
    return state


def recover_dead_sessions(state: AppState) -> bool:
    """Recover dead sessions by recreating them with `claude --continue`.

    For each worktree that still exists on disk, any dead sessions are
    replaced with a new tmux session using `--continue` to resume the
    most recent CC conversation in that directory.

    Returns True if any sessions were recovered.
    """
    changed = False
    for wt in state.worktrees:
        if not Path(wt.path).exists():
            continue
        alive = []
        dead_claude = []
        dead_other = []
        for s in wt.sessions:
            if is_session_alive(s.tmux_session_name):
                alive.append(s)
            elif s.session_type == "claude":
                dead_claude.append(s)
            else:
                dead_other.append(s)
        if not dead_claude and not dead_other:
            continue
        logger.info(
            "Recovering dead sessions in worktree",
            extra={"worktree": wt.name, "dead_claude": len(dead_claude), "dead_other": len(dead_other), "alive": len(alive)},
        )
        # Resume dead claude sessions; drop dead terminal sessions (nothing to resume)
        new_sessions = list(alive)
        if dead_claude:
            resumed = create_session(wt, label="(resumed)", skip_permissions=False, resume=True)
            new_sessions.append(resumed)
        wt.sessions = new_sessions
        changed = True
    return changed


def reconcile_state(state: AppState, config: ResolvedConfig | None = None) -> bool:
    """Prune worktrees whose paths no longer exist, discover new ones. Returns True if changed."""
    changed = False

    valid_worktrees = []
    for wt in state.worktrees:
        if not Path(wt.path).exists():
            changed = True
            continue
        valid_worktrees.append(wt)
    state.worktrees = valid_worktrees
    prune_git_cache({wt.path for wt in valid_worktrees})

    # Discover worktrees on disk that aren't in state
    if config is not None:
        known_paths = {wt.path for wt in state.worktrees}
        for wt in discover_worktrees(config):
            if wt.path not in known_paths:
                logger.info("Discovered worktree on disk", extra={"name": wt.name, "path": wt.path})
                state.worktrees.append(wt)
                changed = True

    return changed


def update_projects_registry(config: ResolvedConfig) -> None:
    """Track this repo in the global projects registry."""
    _ensure_state_dir()
    registry_path = STATE_DIR / "projects.json"
    lock_file = registry_path.with_suffix(".lock")
    with open(lock_file, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            projects: list[str] = []
            if registry_path.exists():
                try:
                    projects = json.loads(registry_path.read_text())
                except (json.JSONDecodeError, TypeError):
                    projects = []
            repo_str = str(config.repo_root)
            if repo_str not in projects:
                projects.append(repo_str)
                registry_path.write_text(json.dumps(projects, indent=2))
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def load_projects_registry() -> list[str]:
    """Load list of known repo paths."""
    registry_path = STATE_DIR / "projects.json"
    if not registry_path.exists():
        return []
    try:
        return json.loads(registry_path.read_text())
    except (json.JSONDecodeError, TypeError):
        return []
