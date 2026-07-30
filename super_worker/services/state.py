import fcntl
import json
import logging
import shutil
from contextlib import contextmanager
from pathlib import Path

from super_worker.config import ResolvedConfig, detect_repo_root
from super_worker.constants import STATE_DIR
from super_worker.models import AppState
from super_worker.services.tmux import build_process_cmd, build_session_env_cmd, create_session, is_session_alive, respawn_pane, _get_server
from super_worker.services.worktree import discover_worktrees, get_current_branch, prune_git_cache

logger = logging.getLogger(__name__)


def _ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


@contextmanager
def _file_lock(path: Path, exclusive: bool = True):
    """Acquire a file lock (shared or exclusive) with automatic cleanup."""
    lock_file = path.with_suffix(".lock")
    with open(lock_file, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _state_file_for(config: ResolvedConfig) -> Path:
    """Per-repo state file keyed by repo root path hash."""
    return STATE_DIR / f"state-{config.state_hash}.json"


def _migrate_data(data: dict) -> dict:
    """Handle backward-compatible field renames."""
    if "repo_path" in data and "repo_root" not in data:
        data["repo_root"] = data.pop("repo_path")
    return data


def _resolve_state_file(config: ResolvedConfig) -> Path:
    """Pick the state file to read: per-repo, or a matching legacy state.json."""
    state_file = _state_file_for(config)
    legacy_file = STATE_DIR / "state.json"
    if not state_file.exists() and legacy_file.exists():
        try:
            legacy_data = json.loads(legacy_file.read_text())
            legacy_root = legacy_data.get("repo_root") or legacy_data.get("repo_path", "")
            if str(config.repo_root) == legacy_root:
                return legacy_file
        except (json.JSONDecodeError, OSError):
            logger.debug("Failed to read legacy state file, starting fresh")
    return state_file


def _read_state_unlocked(config: ResolvedConfig) -> AppState:
    """Read + validate state (caller holds the lock). Falls back on corruption."""
    state_file = _resolve_state_file(config)
    if not state_file.exists():
        return AppState(
            repo_root=str(config.repo_root),
            worktree_base=str(config.base_dir),
        )

    def _parse(path: Path) -> AppState:
        # Raises on ANY corruption (bad JSON *or* wrong shape, e.g. `{}`).
        return AppState.model_validate(_migrate_data(json.loads(path.read_text())))

    try:
        return _parse(state_file)
    except Exception:
        logger.warning("State file corrupted or invalid, falling back to backup", exc_info=True)
    bak = state_file.with_suffix(".bak")
    if bak.exists():
        try:
            return _parse(bak)
        except Exception:
            logger.warning("Backup also corrupted, starting fresh", exc_info=True)
    return AppState(
        repo_root=str(config.repo_root),
        worktree_base=str(config.base_dir),
    )


def _write_state_unlocked(state: AppState, config: ResolvedConfig) -> None:
    """Atomically write state (caller holds the lock)."""
    state_file = _state_file_for(config)
    if state_file.exists():
        shutil.copy2(state_file, state_file.with_suffix(".bak"))
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(state.model_dump_json(indent=2))
    tmp.rename(state_file)


def load_state(config: ResolvedConfig) -> AppState:
    _ensure_state_dir()
    with _file_lock(_state_file_for(config), exclusive=False):
        return _read_state_unlocked(config)


def save_state(state: AppState, config: ResolvedConfig) -> None:
    _ensure_state_dir()
    with _file_lock(_state_file_for(config)):
        _write_state_unlocked(state, config)


@contextmanager
def mutate_state(config: ResolvedConfig):
    """Load → mutate → save under ONE held exclusive lock.

    Prevents lost updates when multiple `sw` processes touch the same
    project's state (e.g. two `sw new` in parallel, or `sw add` while the
    TUI saves): the read and the write can't interleave with another
    writer's cycle. Yields the freshly-read AppState; mutate it in place.
    """
    _ensure_state_dir()
    with _file_lock(_state_file_for(config)):
        state = _read_state_unlocked(config)
        yield state
        _write_state_unlocked(state, config)


def _conversation_exists(worktree_path: str, session_id: str) -> bool:
    """Does Claude Code have a stored conversation with this id for this cwd?

    Claude Code stores each conversation at
    ``~/.claude/projects/<cwd-with-slashes-as-dashes>/<session_id>.jsonl``.
    A stored session_id can point at NOTHING — e.g. a session that was created
    (``--session-id <uuid>``) but never actually used, so Claude never wrote a
    file. Resuming that with ``--resume <uuid>`` opens an empty conversation
    ("wrong session"); callers should fall back to ``--continue`` instead.
    """
    if not session_id:
        return False
    try:
        munged = str(Path(worktree_path).resolve()).replace("/", "-")
        conv = Path.home() / ".claude" / "projects" / munged / f"{session_id}.jsonl"
        return conv.exists()
    except OSError:
        return False


def remove_worktree_from_state(state: AppState, name: str) -> AppState:
    state.worktrees = [wt for wt in state.worktrees if wt.name != name]
    return state


def remove_session_from_state(state: AppState, worktree_name: str, session_id: str) -> AppState:
    wt = state.get_worktree(worktree_name)
    if wt:
        wt.sessions = [s for s in wt.sessions if s.id != session_id]
    return state


def recover_dead_sessions(state: AppState) -> bool:
    """Recover dead sessions by respawning or recreating with `claude --continue`.

    For claude sessions where the tmux session still exists (remain-on-exit),
    respawn the pane in-place to preserve scrollback. Otherwise, create a new
    session. Dead terminal sessions are dropped (nothing to resume).

    Returns True if any sessions were recovered.
    """
    if state.ui_mode == "fast":
        # Fast mode: panes are ephemeral. Dead panes are cleaned up on next launch.
        return False
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
        # Names already handed out this batch (alive + recreated), so a fresh
        # recreate can't collide with a sibling — the bug that produced two
        # sessions sharing one tmux name and resuming the wrong conversation.
        reserved = {s.tmux_session_name for s in alive}
        for s in dead_claude:
            # Decide how to bring THIS session back — per-session, never a
            # blanket --continue (which would give every session in the
            # worktree the same latest conversation).
            sid = s.claude_session_id
            if sid and _conversation_exists(wt.path, sid):
                mode = "resume"        # its conversation exists → --resume <id>
            elif sid:
                mode = "fresh_pinned"  # id but no conversation yet → --session-id <id>
            else:
                mode = "continue"      # legacy session with no id → best-effort --continue

            if mode == "resume":
                process_cmd = build_process_cmd(
                    session_type=s.session_type, skip_permissions=s.skip_permissions,
                    resume=True, session_id=sid,
                )
            elif mode == "fresh_pinned":
                process_cmd = build_process_cmd(
                    session_type=s.session_type, skip_permissions=s.skip_permissions,
                    resume=False, session_id=sid,
                )
            else:
                process_cmd = build_process_cmd(
                    session_type=s.session_type, skip_permissions=s.skip_permissions,
                    resume=True, session_id=None,
                )
            resume_cmd = build_session_env_cmd(s.tmux_session_name, process_cmd)

            # Only respawn in place if this dead name is unique in the batch;
            # a duplicated name must be recreated under a fresh unique name.
            if s.tmux_session_name not in reserved and respawn_pane(s.tmux_session_name, resume_cmd):
                logger.info("Respawned dead pane in-place", extra={"session": s.tmux_session_name})
                reserved.add(s.tmux_session_name)
                new_sessions.append(s)
            else:
                # Session gone entirely (or its name collided) — recreate it
                # under a fresh unique name, in the same mode, preserving its
                # conversation id and skip-permissions choice.
                if mode == "resume":
                    resumed = create_session(
                        wt, label="(resumed)", skip_permissions=s.skip_permissions,
                        resume=True, resume_session_id=sid, reserved_names=reserved,
                    )
                elif mode == "fresh_pinned":
                    resumed = create_session(
                        wt, label="(resumed)", skip_permissions=s.skip_permissions,
                        resume=False, force_session_id=sid, reserved_names=reserved,
                    )
                else:
                    resumed = create_session(
                        wt, label="(resumed)", skip_permissions=s.skip_permissions,
                        resume=True, resume_session_id=None, reserved_names=reserved,
                    )
                reserved.add(resumed.tmux_session_name)
                new_sessions.append(resumed)
        wt.sessions = new_sessions
        changed = True
    return changed


def _ensure_remain_on_exit(state: AppState) -> None:
    """Retroactively set remain-on-exit on all existing tmux sessions."""
    try:
        server = _get_server()
        live = {s.session_name: s for s in server.sessions}
    except Exception:
        return
    for wt in state.worktrees:
        for s in wt.sessions:
            tmux_sess = live.get(s.tmux_session_name)
            if tmux_sess is not None:
                try:
                    tmux_sess.set_option("remain-on-exit", "on")
                except Exception:
                    pass


def ensure_default_worktree(state: AppState, config: ResolvedConfig) -> bool:
    """Ensure the 'main' worktree exists, pointing at the repo root.

    Returns True if a new worktree was created (i.e. state changed).
    Both TUI and fast mode call this; callers can add sessions afterward.
    """
    from super_worker.constants import DEFAULT_WORKTREE_NAME
    from super_worker.models import Worktree

    existing = state.get_worktree(DEFAULT_WORKTREE_NAME)
    if existing:
        existing.branch = get_current_branch(str(config.repo_root))
        return False

    branch = get_current_branch(str(config.repo_root))
    wt = Worktree(name=DEFAULT_WORKTREE_NAME, path=str(config.repo_root), branch=branch)
    state.worktrees.insert(0, wt)
    return True


def dedupe_session_names(state: AppState) -> bool:
    """Give every session a unique tmux_session_name. Returns True if changed.

    Two sessions sharing one name (a past bug) both map to the same tmux
    session, so the preview/resume can't tell them apart. Renaming the
    duplicate is safe: it becomes "not alive", so recovery recreates it under
    the fresh name and resumes its OWN conversation via claude_session_id.
    """
    from super_worker.services.tmux import _worktree_scope, tmux_session_name

    seen: set[str] = set()
    changed = False
    for wt in state.worktrees:
        scope = _worktree_scope(wt)
        for s in wt.sessions:
            if s.tmux_session_name not in seen:
                seen.add(s.tmux_session_name)
                continue
            idx = len(wt.sessions)
            while True:
                cand = tmux_session_name(wt.name, idx, scope)
                if cand not in seen:
                    break
                idx += 1
            logger.info("Renamed duplicate session name %s -> %s", s.tmux_session_name, cand)
            s.tmux_session_name = cand
            seen.add(cand)
            changed = True
    return changed


def reconcile_state(state: AppState, config: ResolvedConfig | None = None) -> bool:
    """Prune worktrees whose paths no longer exist, discover new ones. Returns True if changed."""
    changed = dedupe_session_names(state)

    valid_worktrees = []
    for wt in state.worktrees:
        if not Path(wt.path).exists():
            changed = True
            continue
        valid_worktrees.append(wt)
    state.worktrees = valid_worktrees
    prune_git_cache({wt.path for wt in valid_worktrees})

    # Ensure remain-on-exit is set on all existing sessions
    _ensure_remain_on_exit(state)

    # Discover worktrees on disk that aren't in state
    if config is not None:
        known_paths = {wt.path for wt in state.worktrees}
        for wt in discover_worktrees(config):
            if wt.path not in known_paths:
                logger.info("Discovered worktree on disk", extra={"name": wt.name, "path": wt.path})
                state.worktrees.append(wt)
                changed = True

    return changed


def _load_registry_json() -> list[str]:
    """Load projects.json with error handling. Returns empty list on any failure."""
    registry_path = STATE_DIR / "projects.json"
    if not registry_path.exists():
        return []
    try:
        return json.loads(registry_path.read_text())
    except (json.JSONDecodeError, TypeError):
        return []


def _normalize_registry(projects: list[str]) -> list[str]:
    """Resolve any worktree paths to their main repo root and deduplicate.

    Paths that don't exist RIGHT NOW are kept as-is: dropping them would
    permanently forget projects on unmounted volumes / disconnected drives.
    """
    seen: list[str] = []
    for p in projects:
        path = Path(p)
        if path.exists():
            try:
                p = str(detect_repo_root(path))
            except RuntimeError:
                continue  # exists but is no longer a git repo — drop
        if p not in seen:
            seen.append(p)
    return seen


def _write_registry(registry_path: Path, projects: list[str]) -> None:
    """Atomic write — a crash mid-write must not empty the project list."""
    tmp = registry_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(projects, indent=2))
    tmp.rename(registry_path)


def update_projects_registry(config: ResolvedConfig) -> None:
    """Track this repo in the global projects registry."""
    _ensure_state_dir()
    registry_path = STATE_DIR / "projects.json"
    with _file_lock(registry_path):
        projects = _load_registry_json()
        # Normalize: resolve worktrees → main repo.
        projects = _normalize_registry(projects)
        repo_str = str(config.repo_root)
        if repo_str not in projects:
            projects.append(repo_str)
        _write_registry(registry_path, projects)


def remove_from_projects_registry(path: str) -> None:
    """Remove a repo path from the global projects registry."""
    _ensure_state_dir()
    registry_path = STATE_DIR / "projects.json"
    with _file_lock(registry_path):
        projects = [p for p in _load_registry_json() if p != path]
        _write_registry(registry_path, projects)


def load_projects_registry() -> list[str]:
    """Load list of known repo paths."""
    registry_path = STATE_DIR / "projects.json"
    try:
        with _file_lock(registry_path, exclusive=False):
            return _load_registry_json()
    except OSError:
        return _load_registry_json()


def load_and_reconcile(config: ResolvedConfig, ui_mode: str = "tui") -> AppState:
    """Load state, register project, reconcile worktrees, recover dead sessions.

    Saves state if any changes were made. Used by both TUI and fast mode
    startup — ``ui_mode`` records which one, and matters: fast mode sets
    ``state.ui_mode = "fast"`` and nothing ever set it back, which
    permanently disabled TUI crash-recovery after a single fast-mode run.
    """
    state = load_state(config)
    changed = False
    if state.ui_mode != ui_mode:
        state.ui_mode = ui_mode
        changed = True
    update_projects_registry(config)
    changed = reconcile_state(state, config) or changed
    changed = recover_dead_sessions(state) or changed
    if changed:
        save_state(state, config)
    return state
