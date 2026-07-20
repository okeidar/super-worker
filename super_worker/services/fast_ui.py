"""Fast mode — native tmux split panes with zero rendering overhead.

Instead of the Textual TUI capturing and re-rendering pane output, fast mode
runs each Claude Code instance as a native tmux pane.  The terminal emulator
renders directly — zero ANSI parsing, zero hash checks, zero widget repaints.

Mapping:
  Project  → host tmux session (sw-fast-{project_name})
  Worktree → tmux window within host session
  Session  → tmux pane within worktree window
"""

import logging
import os
import shlex
import shutil

import libtmux

from super_worker.config import ResolvedConfig
from super_worker.constants import FAST_SESSION_PREFIX, FAST_STATUS_INTERVAL, format_pane_title, get_session_type_tag
from super_worker.models import AppState, Session, Worktree
from super_worker.services.tmux import SessionState, _STATE_MAP, _get_server, build_process_cmd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Window name parsing
# ---------------------------------------------------------------------------

def worktree_name_from_window(window_name: str) -> str:
    """Extract the worktree name from a tmux window name.

    Window names follow the format: '[! ]name (branch ↑N↓M)[*]'
    """
    return window_name.lstrip("! ").split(" (")[0]


def find_window_for_worktree(
    host: "libtmux.Session", wt_name: str,
) -> "libtmux.Window | None":
    """Find the tmux window corresponding to a worktree name."""
    for w in host.windows:
        if worktree_name_from_window(w.window_name) == wt_name:
            return w
    return None


# ---------------------------------------------------------------------------
# Target resolution — bound tmux commands pass only IDs (%pane/@window/$session)
# ---------------------------------------------------------------------------

def resolve_target(target: str) -> dict | None:
    """Resolve a tmux id to its context (session/window/pane names + cwd).

    Menu keybindings pass ONLY tmux ids: they match ``^[%@$][0-9]+$`` so they
    are shell-safe by construction. Names (which may contain quotes, ``$``,
    backticks — git allows all of these in branch names) are resolved here in
    Python and never travel through a shell string.
    """
    server = _get_server()
    try:
        result = server.cmd(
            "display-message", "-p", "-t", target,
            "-F", "#{session_name}\t#{window_id}\t#{window_name}\t#{pane_id}\t#{pane_current_path}",
        )
        stdout = getattr(result, "stdout", None) or []
        parts = (stdout[0] if stdout else "").split("\t")
        if len(parts) == 5:
            return {
                "session_name": parts[0],
                "window_id": parts[1],
                "window_name": parts[2],
                "pane_id": parts[3],
                "pane_path": parts[4],
            }
    except Exception:
        logger.debug("Failed to resolve tmux target %s", target, exc_info=True)
    return None


def _is_tmux_id(ref: str) -> bool:
    return bool(ref) and ref[0] in "%@$" and ref[1:].isdigit()


def resolve_window_ref(window_ref: str) -> tuple[str, str | None]:
    """Resolve a window reference (id or legacy name) to (worktree_name, cwd).

    Accepts both ``@N`` ids (current bindings) and raw window names (stale
    bindings from a previous sw version still cached in a running tmux
    server). cwd is the window's active pane path when known — used to locate
    the right project regardless of the popup's working directory.
    """
    if _is_tmux_id(window_ref):
        info = resolve_target(window_ref)
        if info:
            return worktree_name_from_window(info["window_name"]), info["pane_path"]
        return "", None
    return worktree_name_from_window(window_ref), None


def find_window_by_id(host: "libtmux.Session", window_id: str) -> "libtmux.Window | None":
    for w in host.windows:
        if w.window_id == window_id:
            return w
    return None


# ---------------------------------------------------------------------------
# Host session naming
# ---------------------------------------------------------------------------

def host_session_name(config: ResolvedConfig) -> str:
    """Deterministic host session name for a project."""
    return f"{FAST_SESSION_PREFIX}-{config.repo_root.name}"


# ---------------------------------------------------------------------------
# Host session lifecycle
# ---------------------------------------------------------------------------

def ensure_host_session(config: ResolvedConfig) -> libtmux.Session:
    """Get or create the host tmux session for a project."""
    name = host_session_name(config)
    server = _get_server()

    try:
        return server.sessions.get(session_name=name)
    except Exception:
        pass

    session = server.new_session(
        session_name=name,
        start_directory=str(config.repo_root),
    )
    _configure_host_session(session, config)
    return session


def _configure_host_session(session: libtmux.Session, config: ResolvedConfig) -> None:
    """Apply polished tmux options, status bar, and keybindings."""
    server = _get_server()
    project_name = config.repo_root.name

    # --- Status bar ---
    session.set_option("status", "on")
    session.set_option("status-interval", str(FAST_STATUS_INTERVAL))
    session.set_option("status-position", "top")

    session.set_option("status-left", f" [sw] {project_name} \u2502 ")
    session.set_option("status-left-length", "40")
    session.set_option("status-left-style", "bold")

    # Right side: periodic refresh + keybinding hint.
    # '#{session_id}' is single-quoted: #() content runs through a shell,
    # where an unquoted $N would expand to an (empty) positional parameter.
    sw = shlex.quote(shutil.which("sw") or "sw")
    session.set_option(
        "status-right",
        f"#({sw} fast-refresh --session '#{{session_id}}' 2>/dev/null) Ctrl+B, Space: menu ",
    )
    session.set_option("status-right-length", "80")

    # --- Styling ---
    session.set_option("status-style", "bg=#1e1e2e,fg=#cdd6f4")
    session.set_option("pane-border-status", "top")
    session.set_option("pane-border-format", " #{pane_title} ")
    session.set_option("pane-active-border-style", "fg=#89b4fa")
    session.set_option("pane-border-style", "fg=#45475a")
    session.set_option("window-status-format", " #I:#W ")
    session.set_option("window-status-current-format", " #I:#W* ")
    session.set_option("window-status-current-style", "bold,fg=#89b4fa")

    # --- Mouse & pane behaviour ---
    session.set_option("mouse", "on")
    session.set_option("remain-on-exit", "on")

    # --- Keybindings ---
    # SECURITY: bound commands carry ONLY tmux ids (%pane/@window/$session,
    # always [%@$]digits — shell-safe). Names are resolved in Python at
    # invoke time. Interpolating #{window_name} here allowed shell injection:
    # window names embed git branch names, and a branch like x'$(cmd)' is
    # legal — picking any menu item would have executed it. The ids are
    # single-quoted because run-shell/popup commands go through a shell,
    # where a bare $N would expand as a positional parameter.
    #
    # Ids also fix cross-project misrouting: --host used to be baked in at
    # bind time (bindings are server-global), so with two fast-mode projects
    # the menu operated on whichever project configured last.
    wid = "'#{window_id}'"
    pid = "'#{pane_id}'"
    sid = "'#{session_id}'"

    # ── Master menu on Ctrl+B, Space ──
    # One discoverable entry point — no conflicts with tmux defaults.
    # display-menu items: "Label", "shortcut-key", "command"
    server.cmd(
        "bind-key", "-T", "prefix", "Space",
        "display-menu", "-T", "#[bold]Super Worker",
        # -- Worktrees --
        "New worktree",       "w", f'display-popup -w 55 -h 14 -E "{sw} fast-wizard new-worktree --session {sid}"',
        "Delete worktree",    "d", f'display-popup -w 55 -h 14 -E "{sw} fast-wizard delete-worktree --window {wid}"',
        "",                   "",  "",
        # -- Sessions --
        "New session (split)", "s", f'display-popup -w 55 -h 12 -E "{sw} fast-wizard new-session --window {wid}"',
        "Kill this pane",     "x", f'confirm-before -p "Kill this session?" "run-shell \\"{sw} fast-kill-pane --pane {pid}\\"; kill-pane"',
        "Rename session",     "r", f'display-popup -w 55 -h 8 -E "{sw} fast-wizard rename-session --pane {pid}"',
        "Resume dead pane",   "c", f'if-shell -F "#{{pane_dead}}" "run-shell \\"{sw} fast-respawn-pane --pane {pid}\\""  "display-message \\"Pane is still alive\\""',
        "",                   "",  "",
        # -- Git --
        "Git: Commit",        "1", f'display-popup -w 60 -h 8 -E "{sw} fast-wizard git-commit --window {wid}"',
        "Git: Push",          "2", f'display-popup -w 60 -h 6 -E "{sw} fast-git push --window {wid}"',
        "Git: Pull",          "3", f'display-popup -w 60 -h 6 -E "{sw} fast-git pull --window {wid}"',
        "Git: Open PR",       "4", f'display-popup -w 60 -h 6 -E "{sw} fast-git pr --window {wid}"',
        "",                   "",  "",
        # -- Projects & settings --
        "Switch project",     "p", f'display-popup -w 60 -h 18 -E "{sw} fast-wizard switch-project"',
        "Open in terminal",   "t", f'run-shell "{sw} fast-open-terminal --session {sid}"',
        "Edit settings",      "e", f'display-popup -w 70 -h 20 -E "{sw} config"',
        "",                   "",  "",
        "Help",               "?", f'display-popup -w 55 -h 32 -E "{sw} fast-help"',
    )

    # Also bind Ctrl+B, g as a direct git shortcut (lowercase g is not a default tmux binding)
    server.cmd(
        "bind-key", "-T", "prefix", "g",
        "display-menu", "-T", "#[bold]Git",
        "Commit", "c", f'display-popup -w 60 -h 8 -E "{sw} fast-wizard git-commit --window {wid}"',
        "Push",   "p", f'display-popup -w 60 -h 6 -E "{sw} fast-git push --window {wid}"',
        "Pull",   "l", f'display-popup -w 60 -h 6 -E "{sw} fast-git pull --window {wid}"',
        "Open PR","r", f'display-popup -w 60 -h 6 -E "{sw} fast-git pr --window {wid}"',
    )


# ---------------------------------------------------------------------------
# Session model creation (shared by wizards and launch)
# ---------------------------------------------------------------------------

def make_fast_session(
    host_name: str,
    label: str,
    session_type: str = "claude",
    prompt: str | None = None,
    skip_permissions: bool = False,
) -> Session:
    """Create a Session model for fast mode."""
    return Session(
        tmux_session_name=host_name,
        label=label,
        session_type=session_type,
        initial_prompt=prompt,
        skip_permissions=skip_permissions,
    )


# ---------------------------------------------------------------------------
# Pane command building
# ---------------------------------------------------------------------------

def build_pane_cmd(
    session: Session, worktree: Worktree, host_name: str,
    resume: bool = False,
) -> str:
    """Build shell command for a CC or terminal pane with fast mode env vars.

    Uses shared build_process_cmd() for the process part, wraps with fast mode env.
    """
    env_prefix = (
        f"SW_SESSION_NAME={shlex.quote(host_name)} "
        f"SW_FAST_MODE=1 "
        f"SW_PANE_LABEL={shlex.quote(session.label)} "
        f"SW_PANE_TYPE={get_session_type_tag(session.session_type)} "
        f"TERM=xterm-256color"
    )
    process_cmd = build_process_cmd(
        session_type=session.session_type,
        skip_permissions=session.skip_permissions,
        prompt=session.initial_prompt,
        resume=resume,
    )
    return f"env {env_prefix} {process_cmd}"


# ---------------------------------------------------------------------------
# Window and pane creation
# ---------------------------------------------------------------------------

def create_worktree_window(
    host: libtmux.Session,
    worktree: Worktree,
    first_session: Session,
    host_name: str,
    resume: bool = False,
) -> tuple[libtmux.Window, str]:
    """Create a tmux window for a worktree. Returns (window, pane_id)."""
    server = _get_server()
    cmd = build_pane_cmd(first_session, worktree, host_name, resume=resume)
    window = host.new_window(
        window_name=f"{worktree.name} ({worktree.branch})",
        start_directory=worktree.path,
        window_shell=cmd,
    )
    pane_id = window.active_pane.pane_id
    server.cmd("select-pane", "-t", pane_id, "-T", format_pane_title(first_session.label, first_session.session_type))
    return window, pane_id


def add_pane_to_window(
    window: libtmux.Window,
    worktree: Worktree,
    session: Session,
    host_name: str,
    resume: bool = False,
) -> str:
    """Split a window to add a new pane. Returns the pane ID."""
    server = _get_server()
    cmd = build_pane_cmd(session, worktree, host_name, resume=resume)
    pane = window.split_window(start_directory=worktree.path, shell=cmd)
    window.select_layout("tiled")
    server.cmd("select-pane", "-t", pane.pane_id, "-T", format_pane_title(session.label, session.session_type))
    return pane.pane_id


# ---------------------------------------------------------------------------
# State detection
# ---------------------------------------------------------------------------

def detect_pane_states(host_name: str, pane_ids: list[str]) -> dict[str, SessionState]:
    """Read per-pane states from the host session environment."""
    server = _get_server()
    try:
        session = server.sessions.get(session_name=host_name)
        env = session.show_environment()
    except Exception:
        return {pid: SessionState.UNKNOWN for pid in pane_ids}

    results: dict[str, SessionState] = {}
    for pane_id in pane_ids:
        key = f"SW_CC_STATE_{pane_id}"
        value = env.get(key, "")
        results[pane_id] = _STATE_MAP.get(value, SessionState.UNKNOWN)
    return results


# ---------------------------------------------------------------------------
# Window name updates (git info + attention)
# ---------------------------------------------------------------------------

def update_window_names(config: ResolvedConfig, state: AppState, host_name: str) -> None:
    """Update window names with git info and attention indicators."""
    from super_worker.services.worktree import get_branch_status, get_worktree_dirty

    server = _get_server()
    try:
        host = server.sessions.get(session_name=host_name)
    except Exception:
        return

    env = host.show_environment()

    # Build lookup from window name → worktree
    windows_by_wt: dict[str, libtmux.Window] = {}
    for w in host.windows:
        windows_by_wt[worktree_name_from_window(w.window_name)] = w

    for wt in state.worktrees:
        window = windows_by_wt.get(wt.name)
        if not window:
            continue

        # Check attention (any pane waiting approval)
        has_attention = False
        for s in wt.sessions:
            if s.tmux_pane_id:
                key = f"SW_CC_STATE_{s.tmux_pane_id}"
                if env.get(key) == "waiting_approval":
                    has_attention = True
                    break

        try:
            status = get_branch_status(wt.path, config.remote, config.main_branch)
            dirty = get_worktree_dirty(wt.path)
            dirty_mark = "*" if dirty else ""
            prefix = "! " if has_attention else ""
            new_name = f"{prefix}{wt.name} ({wt.branch} \u2191{status['ahead']}\u2193{status['behind']}){dirty_mark}"
            window.rename_window(new_name)
        except Exception:
            pass


# _ensure_default_worktree is now shared via state.ensure_default_worktree


# ---------------------------------------------------------------------------
# Launch entry point
# ---------------------------------------------------------------------------

def _attach_or_switch(name: str) -> None:
    """Attach to the host session — or switch the current client to it.

    Inside tmux ($TMUX set — e.g. the switch-project popup), a nested
    ``tmux attach-session`` is refused by tmux; ``switch-client`` is the
    correct verb and makes "Switch project" actually switch.
    """
    if os.environ.get("TMUX"):
        _get_server().cmd("switch-client", "-t", name)
        return
    os.execvp("tmux", ["tmux", "attach-session", "-t", name])


def launch(config: ResolvedConfig, state: AppState) -> None:
    """Main entry point for fast mode.

    Creates or reattaches to a host tmux session with native split panes.
    """
    from super_worker.services.state import ensure_default_worktree, save_state

    name = host_session_name(config)
    server = _get_server()

    # If host session already has content, just reattach
    try:
        existing = server.sessions.get(session_name=name)
        windows = existing.windows
        # A fresh session has one unnamed window; anything else means it's populated
        has_content = len(windows) > 1 or (
            len(windows) == 1 and windows[0].window_name != ""
        )
        if has_content:
            _attach_or_switch(name)
            return
    except Exception:
        pass  # Session doesn't exist yet

    host = ensure_host_session(config)

    # Track the placeholder window created by new_session() so we can kill it later
    placeholder_window_id = host.windows[0].window_id if host.windows else None

    # Ensure default worktree exists (shared with TUI mode)
    ensure_default_worktree(state, config)

    # Populate windows and panes from state.
    # If a session had a pane_id from a previous tmux session that's now gone,
    # it means the Claude process died with that tmux session. Resume with
    # --continue so the conversation picks up where it left off.
    for wt in state.worktrees:
        if not wt.sessions:
            wt.sessions.append(make_fast_session(name, label="session 0"))

        first = wt.sessions[0]
        resume = first.tmux_pane_id is not None and first.session_type == "claude"
        first.tmux_session_name = name
        window, pane_id = create_worktree_window(host, wt, first, name, resume=resume)
        first.tmux_pane_id = pane_id

        for session in wt.sessions[1:]:
            resume = session.tmux_pane_id is not None and session.session_type == "claude"
            session.tmux_session_name = name
            pid = add_pane_to_window(window, wt, session, name, resume=resume)
            session.tmux_pane_id = pid

    # Kill the placeholder window created by new_session()
    if placeholder_window_id and len(host.windows) > 1:
        try:
            for w in host.windows:
                if w.window_id == placeholder_window_id:
                    w.kill()
                    break
        except Exception:
            pass

    # Persist pane IDs and mode
    state.ui_mode = "fast"
    save_state(state, config)

    # Show a welcome message on first launch so users discover the menu.
    # display-message appears at the bottom for 5 seconds, non-intrusive.
    session_ref = shlex.quote(name)
    server.cmd(
        "set-hook", "-t", name, "client-attached",
        f"run-shell 'sleep 0.5 && "
        f"tmux display-message -d 5000 -t {session_ref} "
        f"\" Welcome to Super Worker!  Press Ctrl+B then Space to open the menu.  "
        f"Ctrl+B,g: git | Ctrl+B,n/p: switch tabs\" && "
        f"tmux set-hook -u -t {session_ref} client-attached'",
    )

    # Attach (or switch the current client when already inside tmux)
    _attach_or_switch(name)
