import re
from pathlib import Path

STATE_DIR = Path.home() / ".config" / "sw"
SESSION_STATES_DIR = STATE_DIR / "session-states"

TMUX_SESSION_PREFIX = "sw"
POLL_INTERVAL_MS = 200  # Legacy: used as fallback only
PANE_WATCHER_DEBOUNCE_MS = 16  # Debounce after kqueue signal
PANE_FALLBACK_POLL_S = 0.15  # Poll every 150ms — set_interval fires directly (bypasses message queue)
PANE_ECHO_POLL_S = 0.03  # Re-capture this soon after a keystroke so input echoes without waiting for the fallback tick
PANE_CAPTURE_LINES = 500  # Scrollback lines pulled by the legacy full-buffer capture_pane()
PANE_HISTORY_MAX_LINES = 5000  # Preview scrollback cap — lines accumulated locally per session
PANE_HISTORY_CHUNK_LINES = 200  # History lines per sealed render chunk (parsed once, never re-rendered)
SIDEBAR_REFRESH_S = 5

DEFAULT_WORKTREE_NAME = "main"

# Keys the preview terminal does NOT forward to tmux — they bubble up to the
# app so global bindings (Ctrl+N/S/A/…, project cycling, F12 screenshot) fire.
# Tab is reserved for TUI focus movement (sidebar ⇄ terminal). Deliberately
# NOT reserved: Shift+Tab (Claude Code permission-mode cycling) and Ctrl+R
# (Claude Code's transcript view — the only way to browse a CC session's
# full history, since CC never writes terminal scrollback). Rename is F2.
RESERVED_KEYS = {"ctrl+n", "ctrl+s", "ctrl+a", "ctrl+t", "f2", "ctrl+d", "ctrl+e", "ctrl+q", "ctrl+o", "ctrl+shift+left", "ctrl+shift+right", "f12", "tab"}

_WORKTREE_NAME_RE = re.compile(r"[a-zA-Z0-9_-]+")


def is_valid_worktree_name(name: str) -> bool:
    """Worktree names must be filesystem- and Textual-id-safe.

    Textual builds widget ids like ``wt-<name>``; anything outside this set
    (e.g. ``fix/bug`` or ``my.feature``) crashes the TUI at compose time.
    """
    return bool(name) and _WORKTREE_NAME_RE.fullmatch(name) is not None

FAST_SESSION_PREFIX = "sw-fast"
FAST_STATUS_INTERVAL = 2  # seconds between tmux status bar refreshes


def get_session_type_tag(session_type: str) -> str:
    """Return short tag for a session type: 'sh' for terminal, 'CC' for claude."""
    return "sh" if session_type == "terminal" else "CC"


def format_pane_title(label: str, session_type: str) -> str:
    """Format a pane title like '● label [CC]'."""
    return f"\u25cf {label} [{get_session_type_tag(session_type)}]"
