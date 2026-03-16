import logging
import os
import platform
import shlex
import shutil
import subprocess
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
        # TTL expired — sweep all stale entries at once so sessions that die
        # unexpectedly (crash, external kill) don't linger in the cache forever.
        for k in [k for k, (t, _) in _pane_cache.items() if now - t >= _PANE_CACHE_TTL]:
            del _pane_cache[k]

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
    UNKNOWN = "unknown"


_STATE_MAP = {
    "waiting_input": SessionState.WAITING_INPUT,
    "waiting_approval": SessionState.WAITING_APPROVAL,
    "running": SessionState.RUNNING,
}


def read_state_file(session_name: str) -> SessionState:
    """Read session state from file written by sw-hook.sh. No subprocess calls."""
    from super_worker.constants import SESSION_STATES_DIR
    state_file = SESSION_STATES_DIR / session_name
    try:
        value = state_file.read_text().strip()
        return _STATE_MAP.get(value, SessionState.UNKNOWN)
    except (OSError, ValueError):
        return SessionState.UNKNOWN


def read_all_state_files(session_names: list[str]) -> dict[str, SessionState]:
    """Read state files for multiple sessions. No subprocess calls.

    Falls back to UNKNOWN for sessions without state files (e.g. terminals).
    Does NOT detect dead sessions — callers should use batch_detect_session_states
    for that (e.g. on periodic refresh).
    """
    from super_worker.constants import SESSION_STATES_DIR
    results: dict[str, SessionState] = {}
    for name in session_names:
        state_file = SESSION_STATES_DIR / name
        try:
            value = state_file.read_text().strip()
            results[name] = _STATE_MAP.get(value, SessionState.UNKNOWN)
        except (OSError, ValueError):
            results[name] = SessionState.UNKNOWN
    return results


def verify_waiting_approval(session_names: list[str]) -> dict[str, SessionState]:
    """Cross-check sessions showing waiting_approval against tmux env.

    State files can be stale for sessions started before the latest hook
    script was installed. The tmux env (SW_CC_STATE) is set by both old
    and new hooks, so it's always current. Only called for the subset of
    sessions that appear to be waiting_approval — minimal overhead.

    Returns corrected states. Also fixes the state files so the stale
    state doesn't persist.
    """
    from super_worker.constants import SESSION_STATES_DIR
    if not session_names:
        return {}
    server = _get_server()
    try:
        live = {s.session_name: s for s in server.sessions}
    except Exception:
        return {}
    corrected: dict[str, SessionState] = {}
    for name in session_names:
        session = live.get(name)
        if not session:
            continue
        try:
            env = session.show_environment()
            value = env.get("SW_CC_STATE", "")
            real_state = _STATE_MAP.get(value, SessionState.UNKNOWN)
            if real_state != SessionState.WAITING_APPROVAL:
                corrected[name] = real_state
                # Fix the stale state file
                try:
                    state_file = SESSION_STATES_DIR / name
                    state_file.write_text(value or "")
                except OSError:
                    pass
        except Exception:
            pass
    return corrected


def batch_check_alive(session_names: list[str]) -> set[str]:
    """Return the set of session names that are dead or missing.

    Lightweight alternative to batch_detect_session_states — checks only
    whether sessions exist and their pane is alive.  No show_environment
    calls, so this is a single ``tmux list-sessions`` plus one attribute
    read per session.
    """
    if not session_names:
        return set()

    server = _get_server()
    try:
        live_sessions = {s.session_name: s for s in server.sessions}
    except Exception:
        return set()

    dead: set[str] = set()
    for name in session_names:
        if name not in live_sessions:
            dead.add(name)
            continue
        try:
            pane = live_sessions[name].active_pane
            if pane and getattr(pane, "pane_dead", None) == "1":
                dead.add(name)
        except Exception:
            pass
    return dead


def cleanup_state_file(session_name: str) -> None:
    """Remove the state file for a session (called on session deletion)."""
    from super_worker.constants import SESSION_STATES_DIR
    try:
        (SESSION_STATES_DIR / session_name).unlink(missing_ok=True)
    except OSError:
        pass


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



def build_process_cmd(
    session_type: str = "claude",
    skip_permissions: bool = False,
    prompt: str | None = None,
    resume: bool = False,
) -> str:
    """Build the process command (claude or shell) without env wrapper.

    Shared by both TUI mode (create_session) and fast mode (build_pane_cmd).
    """
    if session_type == "terminal":
        shell = os.environ.get("SHELL", "/bin/bash")
        return shlex.quote(shell)
    base = "claude --dangerously-skip-permissions" if skip_permissions else "claude"
    if resume:
        base = f"{base} --continue"
    elif prompt:
        base = f"{base} {shlex.quote(prompt)}"
    return base


def build_session_env_cmd(session_name: str, process_cmd: str) -> str:
    """Wrap a process command with TUI-mode env vars.

    Shared by create_session() and recover_dead_sessions().
    """
    return f"env SW_SESSION_NAME={shlex.quote(session_name)} TERM=xterm-256color {process_cmd}"


def create_session(
    worktree: Worktree,
    prompt: str | None = None,
    label: str | None = None,
    skip_permissions: bool = False,
    resume: bool = False,
    session_type: str = "claude",
) -> Session:
    """Create a tmux session running claude or a plain shell in the worktree directory."""
    server = _get_server()
    sess_name = _find_available_session_name(worktree)

    if session_type == "terminal":
        session_label = label or "terminal"
    else:
        session_label = label or prompt or f"session {len(worktree.sessions)}"

    process_cmd = build_process_cmd(session_type, skip_permissions, prompt, resume)
    cmd = build_session_env_cmd(sess_name, process_cmd)

    tmux_session = server.new_session(
        session_name=sess_name,
        start_directory=worktree.path,
        window_command=cmd,
    )
    tmux_session.set_option("mouse", "on")
    tmux_session.set_option("remain-on-exit", "on")

    session = Session(
        tmux_session_name=sess_name,
        label=session_label,
        session_type=session_type,
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
    """Check if a tmux session exists and its pane is alive."""
    try:
        session = _get_server().sessions.get(session_name=tmux_session_name)
        pane = session.active_pane
        return pane is not None and getattr(pane, "pane_dead", None) != "1"
    except Exception:
        return False


def detect_session_state(session_name: str) -> SessionState:
    """Detect state for a single session using raw subprocess (~7ms vs ~29ms via libtmux)."""
    pane = _get_pane(session_name)
    if pane is None:
        return SessionState.DEAD
    try:
        if getattr(pane, "pane_dead", None) == "1":
            return SessionState.DEAD
        # Raw subprocess is ~4x faster than libtmux's show_environment()
        result = subprocess.run(
            ["tmux", "show-environment", "-t", session_name, "SW_CC_STATE"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode != 0:
            return SessionState.UNKNOWN
        # Output format: "SW_CC_STATE=waiting_input" or "-SW_CC_STATE" (unset)
        line = result.stdout.strip()
        if "=" in line:
            value = line.split("=", 1)[1]
            return _STATE_MAP.get(value, SessionState.UNKNOWN)
        return SessionState.UNKNOWN
    except Exception:
        return SessionState.UNKNOWN


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

        session = live_sessions[name]

        # Check if the pane is dead (remain-on-exit keeps session alive)
        try:
            pane = session.active_pane
            if pane and getattr(pane, "pane_dead", None) == "1":
                results[name] = SessionState.DEAD
                continue
        except Exception:
            pass

        try:
            env = session.show_environment()
            value = env.get("SW_CC_STATE", "")
            results[name] = _STATE_MAP.get(value, SessionState.UNKNOWN)
        except Exception:
            results[name] = SessionState.UNKNOWN

    return results


def has_waiting_approval(states: dict[str, SessionState]) -> bool:
    """Check if any session state is WAITING_APPROVAL."""
    return any(v == SessionState.WAITING_APPROVAL for v in states.values())


def respawn_pane(tmux_session_name: str, cmd: str) -> bool:
    """Respawn a dead pane with a new command. Returns True if successful."""
    try:
        server = _get_server()
        server.cmd("respawn-pane", "-k", "-t", tmux_session_name, cmd)
        invalidate_pane_cache(tmux_session_name)
        return True
    except Exception:
        logger.debug("Failed to respawn pane for session %s", tmux_session_name)
        return False


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


def open_external_terminal(tmux_session_name: str) -> bool:
    """Open a new terminal emulator window attached to a tmux session.

    Returns True if a terminal was launched, False if no emulator was found.
    """
    attach_cmd = f"tmux attach-session -t {shlex.quote(tmux_session_name)}"
    system = platform.system()
    if system == "Darwin":
        subprocess.Popen([
            "osascript", "-e",
            f'tell application "Terminal" to do script "{attach_cmd}"',
        ])
        return True
    else:
        for term in ("x-terminal-emulator", "gnome-terminal", "xterm"):
            if shutil.which(term):
                subprocess.Popen([term, "-e", "bash", "-c", attach_cmd])
                return True
    return False
