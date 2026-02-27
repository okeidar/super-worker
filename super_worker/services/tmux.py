import logging
import shlex
import time
from enum import Enum

import libtmux

from super_worker.constants import TMUX_SESSION_PREFIX
from super_worker.models import Session, Worktree

logger = logging.getLogger(__name__)

# Cached server and pane references to avoid repeated subprocess calls.
# libtmux.Server() is cheap, but .sessions.get() triggers `tmux list-sessions`.
_server: libtmux.Server | None = None
_pane_cache: dict[str, tuple[float, libtmux.Pane]] = {}  # session_name -> (timestamp, pane)
_PANE_CACHE_TTL = 30.0  # seconds


def _get_server() -> libtmux.Server:
    global _server
    if _server is None:
        _server = libtmux.Server()
    return _server


def _get_pane(session_name: str) -> libtmux.Pane | None:
    """Get cached pane reference, refreshing if stale."""
    now = time.monotonic()
    if session_name in _pane_cache:
        ts, pane = _pane_cache[session_name]
        if now - ts < _PANE_CACHE_TTL:
            return pane

    server = _get_server()
    try:
        session = server.sessions.get(session_name=session_name)
        pane = session.active_pane
        _pane_cache[session_name] = (now, pane)
        return pane
    except Exception:
        _pane_cache.pop(session_name, None)
        return None


def invalidate_pane_cache(session_name: str | None = None) -> None:
    """Clear cached pane references."""
    if session_name:
        _pane_cache.pop(session_name, None)
    else:
        _pane_cache.clear()


class SessionState(Enum):
    DEAD = "dead"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    WAITING_APPROVAL = "waiting_approval"


_STATE_MAP = {
    "waiting_input": SessionState.WAITING_INPUT,
    "waiting_approval": SessionState.WAITING_APPROVAL,
    "running": SessionState.RUNNING,
}


def tmux_session_name(worktree_name: str, index: int) -> str:
    return f"{TMUX_SESSION_PREFIX}-{worktree_name}-{index}"


def _find_available_session_name(worktree: Worktree) -> str:
    """Find next available tmux session name, avoiding collisions."""
    server = _get_server()
    existing = {s.session_name for s in server.sessions}
    index = len(worktree.sessions)
    for _ in range(1000):
        name = tmux_session_name(worktree.name, index)
        if name not in existing:
            return name
        index += 1
    raise RuntimeError(f"Could not find available session name for worktree '{worktree.name}'")


def create_session(
    worktree: Worktree,
    prompt: str | None = None,
    label: str | None = None,
    skip_permissions: bool = False,
    resume: bool = False,
) -> Session:
    """Create a tmux session running claude in the worktree directory."""
    server = _get_server()
    sess_name = _find_available_session_name(worktree)

    session_label = label or prompt or f"session {len(worktree.sessions)}"

    base = "claude --dangerously-skip-permissions" if skip_permissions else "claude"
    if resume:
        base = f"{base} --continue"
    elif prompt:
        base = f"{base} {shlex.quote(prompt)}"
    cmd = f"env SW_SESSION_NAME={shlex.quote(sess_name)} TERM=xterm-256color {base}"

    tmux_session = server.new_session(
        session_name=sess_name,
        start_directory=worktree.path,
        window_command=cmd,
    )
    tmux_session.set_option("mouse", "on")

    session = Session(
        tmux_session_name=sess_name,
        label=session_label,
        initial_prompt=prompt,
        skip_permissions=skip_permissions,
    )
    return session


def capture_pane(tmux_session_name: str) -> str:
    """Capture pane content with scrollback history and ANSI escapes."""
    pane = _get_pane(tmux_session_name)
    if pane is None:
        return f"[Session {tmux_session_name} not found]"
    try:
        lines = pane.capture_pane(start=-500, escape_sequences=True)
        return "\n".join(lines)
    except Exception:
        invalidate_pane_cache(tmux_session_name)
        return f"[Session {tmux_session_name} not found]"


def send_keys(tmux_session_name: str, *keys: str, literal: bool = False) -> None:
    """Send keystrokes to a tmux session."""
    pane = _get_pane(tmux_session_name)
    if pane is None:
        logger.debug("Failed to send keys to tmux session", extra={"session": tmux_session_name})
        return
    try:
        for key in keys:
            pane.send_keys(key, enter=False, literal=literal)
    except Exception:
        invalidate_pane_cache(tmux_session_name)
        logger.debug("Failed to send keys to tmux session", extra={"session": tmux_session_name})


def is_session_alive(tmux_session_name: str) -> bool:
    """Check if a tmux session exists."""
    try:
        _get_server().sessions.get(session_name=tmux_session_name)
        return True
    except Exception:
        return False


def batch_detect_session_states(session_names: list[str]) -> dict[str, SessionState]:
    """Detect states for multiple sessions using the libtmux API directly."""
    if not session_names:
        return {}

    server = _get_server()
    try:
        live_sessions = {s.session_name: s for s in server.sessions}
    except Exception:
        logger.debug("Failed to list tmux sessions for batch state detection", exc_info=True)
        live_sessions = {}

    results: dict[str, SessionState] = {}
    for name in session_names:
        if name not in live_sessions:
            results[name] = SessionState.DEAD
            continue

        try:
            env = live_sessions[name].show_environment()
            value = env.get("SW_CC_STATE", "")
            results[name] = _STATE_MAP.get(value, SessionState.RUNNING)
        except Exception:
            results[name] = SessionState.RUNNING

    return results


def enable_mouse(tmux_session_name: str) -> None:
    """Enable mouse support on a tmux session."""
    try:
        session = _get_server().sessions.get(session_name=tmux_session_name)
        session.set_option("mouse", "on")
    except Exception:
        logger.debug("Failed to enable mouse on tmux session", extra={"session": tmux_session_name})


def kill_session(tmux_session_name: str) -> None:
    """Kill a tmux session."""
    try:
        session = _get_server().sessions.get(session_name=tmux_session_name)
        session.kill()
    except Exception:
        logger.debug("Failed to kill tmux session", extra={"session": tmux_session_name})
    invalidate_pane_cache(tmux_session_name)


def kill_all_sessions(worktree: Worktree) -> None:
    """Kill all tmux sessions for a worktree."""
    for session in worktree.sessions:
        kill_session(session.tmux_session_name)
