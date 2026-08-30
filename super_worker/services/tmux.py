import hashlib
import logging
import os
import platform
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from enum import Enum

import libtmux

from super_worker.constants import PANE_CAPTURE_LINES, PANE_HISTORY_MAX_LINES, TMUX_SESSION_PREFIX
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


def tmux_session_name(worktree_name: str, index: int, scope: str = "") -> str:
    """Build a tmux session name.

    ``scope`` is a per-project tag: all sw sessions share one tmux server, so
    two projects that each have a 'main' worktree would otherwise generate the
    same ``sw-main-0`` name — one project's preview would then attach to the
    other's session. Scoping by the worktree path makes names globally unique.
    """
    if scope:
        return f"{TMUX_SESSION_PREFIX}-{worktree_name}-{scope}-{index}"
    return f"{TMUX_SESSION_PREFIX}-{worktree_name}-{index}"


def _worktree_scope(worktree: Worktree) -> str:
    """Short, stable, globally-unique tag for a worktree (derived from its path).

    The path uniquely identifies the worktree across all projects, so hashing
    it disambiguates same-named worktrees in different repos without threading
    project config through every create_session caller.
    """
    return hashlib.sha256(str(worktree.path).encode()).hexdigest()[:6]


def _find_available_session_name(worktree: Worktree, reserved: set[str] | None = None) -> str:
    """Find next available tmux session name, avoiding collisions.

    Reserves three sources, not just live tmux sessions: also the names
    already assigned to this worktree's OTHER sessions in state, and any
    ``reserved`` names (e.g. sessions created earlier in the same recovery
    loop that aren't committed yet). Without the state check, two sessions in
    one worktree could be handed the same name — then both map to one tmux
    session and their previews/resumes get confused.
    """
    server = _get_server()
    taken: set[str] = set()
    try:
        taken |= {s.session_name for s in server.sessions}
    except Exception:
        pass
    taken |= {s.tmux_session_name for s in worktree.sessions}
    if reserved:
        taken |= reserved
    scope = _worktree_scope(worktree)
    index = len(worktree.sessions)
    for _ in range(10000):
        name = tmux_session_name(worktree.name, index, scope)
        if name not in taken:
            return name
        index += 1
    raise RuntimeError(f"Could not find available session name for worktree '{worktree.name}'")



def _default_session_label(sess_name: str) -> str:
    """Default label derived from the session's unique tmux index.

    ``len(worktree.sessions)`` is NOT unique — deleting then adding a session
    repeats the count and yields duplicate labels (two "session 1"), which are
    indistinguishable in the sidebar. The tmux name's trailing index is already
    allocated to be unique per worktree (see ``_find_available_session_name``),
    so reuse it for a stable, collision-free label.
    """
    tail = sess_name.rsplit("-", 1)[-1]
    return f"session {tail}" if tail.isdigit() else "session"


def build_process_cmd(
    session_type: str = "claude",
    skip_permissions: bool = False,
    prompt: str | None = None,
    resume: bool = False,
    session_id: str | None = None,
) -> str:
    """Build the process command (claude or shell) without env wrapper.

    Shared by both TUI mode (create_session) and fast mode (build_pane_cmd).

    When ``resume`` is set, a known ``session_id`` resumes that exact
    conversation (``--resume <id>``); without one we fall back to the blunt
    ``--continue``. For a fresh launch, ``session_id`` pins the new
    conversation's id (``--session-id <uuid>``) so it can be resumed precisely
    later.
    """
    if session_type == "terminal":
        shell = os.environ.get("SHELL", "/bin/bash")
        return shlex.quote(shell)
    base = "claude --dangerously-skip-permissions" if skip_permissions else "claude"
    if resume:
        if session_id:
            base = f"{base} --resume {shlex.quote(session_id)}"
        else:
            base = f"{base} --continue"
    else:
        if session_id:
            base = f"{base} --session-id {shlex.quote(session_id)}"
        if prompt:
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
    resume_session_id: str | None = None,
    force_session_id: str | None = None,
    reserved_names: set[str] | None = None,
) -> Session:
    """Create a tmux session running claude or a plain shell in the worktree directory.

    Every claude session carries a pinned conversation id so it can always be
    resumed as ITSELF — critical when a worktree has several sessions, where
    ``--continue`` (latest only) would collapse them all onto one conversation:
      * fresh launch        → new uuid, ``--session-id <uuid>``
      * resume=True + id     → ``--resume <id>`` (that exact conversation)
      * force_session_id     → fresh but pinned to a specific id (used by
                               recovery for a session whose conversation has
                               no file yet — keeps its id stable, no hijack)
    The chosen id is stored on the returned Session.

    ``reserved_names`` lets a caller (recovery) exclude names it has already
    handed out in the same batch but not yet committed to state.
    """
    server = _get_server()
    sess_name = _find_available_session_name(worktree, reserved=reserved_names)

    if session_type == "terminal":
        session_label = label or "terminal"
        session_id = None
    else:
        session_label = label or prompt or _default_session_label(sess_name)
        if resume:
            session_id = resume_session_id
        else:
            session_id = force_session_id or str(uuid.uuid4())

    process_cmd = build_process_cmd(session_type, skip_permissions, prompt, resume, session_id=session_id)
    cmd = build_session_env_cmd(sess_name, process_cmd)

    tmux_session = server.new_session(
        session_name=sess_name,
        start_directory=worktree.path,
        window_command=cmd,
    )
    tmux_session.set_option("mouse", "on")
    tmux_session.set_option("remain-on-exit", "on")
    # Deeper scrollback for panes created later in this session (respawn /
    # recovery) — tmux reads history-limit at pane creation time, so the
    # initial pane keeps the global default.
    tmux_session.set_option("history-limit", str(PANE_HISTORY_MAX_LINES))

    session = Session(
        tmux_session_name=sess_name,
        label=session_label,
        session_type=session_type,
        initial_prompt=prompt,
        skip_permissions=skip_permissions,
        claude_session_id=session_id,
    )
    return session


@dataclass
class PaneSnapshot:
    """A capture of a pane's content plus its cursor geometry.

    ``text`` is the raw capture (with ANSI escapes). Cursor fields let the
    preview overlay a cursor at the same cell the real terminal shows it.
    ``history_size`` is the number of scrollback lines above the visible
    screen — the cursor's captured line is ``min(history_size, capture
    window) + cursor_y``, which stays correct even when tmux trims trailing
    blank lines from the capture.
    """

    text: str
    cursor_x: int = 0
    cursor_y: int = 0
    pane_height: int = 0
    history_size: int = 0
    cursor_visible: bool = False
    # True when the program in the pane requested mouse reporting (e.g.
    # Claude Code, vim, less). The preview then forwards mouse events into
    # the pane instead of scrolling its own container.
    mouse_any: bool = False


def capture_pane(tmux_session_name: str) -> str:
    """Capture pane content with scrollback history and ANSI escapes."""
    pane = _get_pane(tmux_session_name)
    if pane is None:
        return f"[Session {tmux_session_name} not found]"
    try:
        lines = pane.capture_pane(start=-PANE_CAPTURE_LINES, escape_sequences=True)
        return "\n".join(lines)
    except Exception:
        invalidate_pane_cache(tmux_session_name)
        return f"[Session {tmux_session_name} not found]"


def capture_pane_snapshot(tmux_session_name: str) -> PaneSnapshot:
    """Capture the VISIBLE screen and cursor position.

    Scrollback is not included — history is drained incrementally via
    ``capture_history_tail`` (history lines are immutable once pushed, so the
    preview parses each line exactly once instead of re-capturing a whole
    window every refresh). ``cursor_visible`` is False when the cursor is
    hidden (DECTCEM) or the pane is in copy/scroll mode.
    """
    pane = _get_pane(tmux_session_name)
    if pane is None:
        return PaneSnapshot(text=f"[Session {tmux_session_name} not found]")
    try:
        lines = pane.capture_pane(escape_sequences=True)
        text = "\n".join(lines)
    except Exception:
        invalidate_pane_cache(tmux_session_name)
        return PaneSnapshot(text=f"[Session {tmux_session_name} not found]")

    cursor_x = cursor_y = pane_height = history_size = 0
    cursor_visible = mouse_any = False
    try:
        # Query via the same server connection as the capture — a raw
        # `tmux` subprocess could hit a different socket than libtmux.
        result = _get_server().cmd(
            "display-message", "-p", "-t", tmux_session_name,
            "-F", "#{cursor_x},#{cursor_y},#{cursor_flag},#{pane_height},#{pane_in_mode},#{history_size},#{mouse_any_flag}",
        )
        stdout = getattr(result, "stdout", None) or []
        line = stdout[0].strip() if stdout else ""
        parts = line.split(",")
        if len(parts) == 7:
            cursor_x = int(parts[0] or 0)
            cursor_y = int(parts[1] or 0)
            pane_height = int(parts[3] or 0)
            history_size = int(parts[5] or 0)
            cursor_visible = parts[2] == "1" and parts[4] == "0"
            mouse_any = parts[6] == "1"
    except Exception:
        pass

    return PaneSnapshot(
        text=text,
        cursor_x=cursor_x,
        cursor_y=cursor_y,
        pane_height=pane_height,
        history_size=history_size,
        cursor_visible=cursor_visible,
        mouse_any=mouse_any,
    )


def capture_history_tail(tmux_session_name: str, count: int) -> str | None:
    """Capture the ``count`` NEWEST scrollback lines (the ones most recently
    pushed off the visible screen), with ANSI escapes.

    Used to drain history incrementally: when ``history_size`` grows by D
    since the last drain, lines ``-D..-1`` are exactly the new ones. Returns
    None on failure so callers can retry next poll without advancing their
    consumed counter.
    """
    if count <= 0:
        return ""
    pane = _get_pane(tmux_session_name)
    if pane is None:
        return None
    try:
        lines = pane.capture_pane(start=-count, end=-1, escape_sequences=True)
        return "\n".join(lines)
    except Exception:
        invalidate_pane_cache(tmux_session_name)
        return None


def resize_window(tmux_session_name: str, width: int, height: int) -> None:
    """Resize a session's window to match the preview widget.

    Requires ``window-size manual`` (see ``set_window_size``); otherwise tmux
    auto-sizes detached sessions and ignores the request.
    """
    if width <= 0 or height <= 0:
        return
    try:
        _get_server().cmd(
            "resize-window", "-t", tmux_session_name,
            "-x", str(width), "-y", str(height),
        )
    except Exception:
        logger.debug("Failed to resize window for %s", tmux_session_name)


def set_window_size(tmux_session_name: str, mode: str) -> None:
    """Set the session's window-size option ('manual' | 'latest' | 'largest')."""
    try:
        session = _get_server().sessions.get(session_name=tmux_session_name)
        session.set_option("window-size", mode)
    except Exception:
        logger.debug("Failed to set window-size for %s", tmux_session_name)


def paste_to_pane(tmux_session_name: str, text: str) -> None:
    """Paste ``text`` into a pane using tmux bracketed paste.

    Sending multi-line text as literal keystrokes makes every embedded newline
    submit Claude Code's prompt early. ``paste-buffer -p`` wraps the text in
    bracketed-paste markers *only if the pane's app requested that mode* — so
    Claude receives it as one paste, while a plain shell still gets clean text.
    """
    if not text:
        return
    server = _get_server()
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".swpaste", delete=False, encoding="utf-8"
        ) as f:
            f.write(text)
            tmp = f.name
        server.cmd("load-buffer", "-b", "sw-paste", tmp)
        server.cmd("paste-buffer", "-p", "-d", "-b", "sw-paste", "-t", tmux_session_name)
    except Exception:
        invalidate_pane_cache(tmux_session_name)
        logger.debug("Failed to paste into tmux session", extra={"session": tmux_session_name})
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


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
    """Respawn a dead pane with a new command. Returns True if successful.

    libtmux's ``Server.cmd`` does NOT raise when the tmux command fails (e.g.
    the session is gone entirely, not just the pane) — the error only lands in
    ``result.stderr``. So we must inspect stderr; otherwise a failed respawn
    reports success and the caller never falls back to recreating the session.
    """
    try:
        server = _get_server()
        result = server.cmd("respawn-pane", "-k", "-t", tmux_session_name, cmd)
        invalidate_pane_cache(tmux_session_name)
    except Exception:
        logger.debug("Failed to respawn pane for session %s", tmux_session_name)
        return False
    stderr = getattr(result, "stderr", None)
    if isinstance(stderr, (list, tuple)):
        if any(line.strip() for line in stderr):
            logger.debug("respawn-pane failed for %s: %s", tmux_session_name, stderr)
            return False
    elif isinstance(stderr, str) and stderr.strip():
        logger.debug("respawn-pane failed for %s: %s", tmux_session_name, stderr)
        return False
    return True


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
        # Escape for the AppleScript string literal — shlex quoting alone
        # doesn't prevent breaking out of the surrounding double quotes.
        as_escaped = attach_cmd.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.Popen([
            "osascript", "-e",
            f'tell application "Terminal" to do script "{as_escaped}"',
        ])
        return True
    else:
        for term in ("x-terminal-emulator", "gnome-terminal", "xterm"):
            if shutil.which(term):
                subprocess.Popen([term, "-e", "bash", "-c", attach_cmd])
                return True
    return False
