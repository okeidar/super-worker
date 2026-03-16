import concurrent.futures
import logging
import re

from rich.text import Text
from textual.events import Click, Key, Paste
from textual.message import Message
from textual.reactive import reactive
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Static
from textual.worker import Worker, WorkerState

from super_worker.constants import PANE_FALLBACK_POLL_S, RESERVED_KEYS
from super_worker.services.pane_watcher import PaneWatcher
from super_worker.services.tmux import capture_pane, send_keys

logger = logging.getLogger(__name__)

# Strip ANSI background color sequences to avoid theme bleed
_BG_ANSI_RE = re.compile(r"\x1b\[(?:4[0-9]|10[0-7]|48;[0-9;]*)m")

# Sentinel that never equals any real hash — avoids the hash("") == 0 bug
_NO_HASH = object()

# If no successful render for this long, force a re-capture (empty screen recovery)
_FORCE_REFRESH_S = 3.0


class TerminalPane(Widget, can_focus=True):
    """Displays captured tmux pane content and forwards keystrokes.

    This is a preview — for full interaction (cursor, scrolling, CC UI),
    press Ctrl+A to attach directly to the tmux session.
    """

    class StateChanged(Message):
        """Posted when a session's state changes (detected via kqueue on state file)."""

        def __init__(self, session_name: str) -> None:
            self.session_name = session_name
            super().__init__()


    active_session: reactive[str | None] = reactive(None)

    DEFAULT_CSS = """
    TerminalPane {
        width: 1fr;
        height: 1fr;
        padding: 0 1;
        layout: vertical;
    }
    TerminalPane:focus {
        border: tall $accent;
    }
    #terminal-scroll {
        width: 1fr;
        height: 1fr;
        overflow-y: auto;
    }
    #terminal-content {
        width: 1fr;
        height: auto;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._last_hash: int | object = _NO_HASH
        self._fallback_timer = None
        self._last_successful_render: float = 0.0
        self._watcher = PaneWatcher()
        self._watched_state_sessions: set[str] = set()
        # Single-thread executor guarantees FIFO key delivery.
        # run_worker without exclusive=True spawns concurrent workers that can
        # race each other, causing characters to arrive at tmux out of order.
        self._send_keys_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="sw-send-keys"
        )

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="terminal-scroll"):
            content = Static("Select a session · Ctrl+A to attach", id="terminal-content")
            content.auto_links = False
            content.ALLOW_SELECT = False
            yield content

    def watch_active_session(self, old_value: str | None, session_name: str | None) -> None:
        if self._fallback_timer is not None:
            self._fallback_timer.stop()
            self._fallback_timer = None
        self._last_hash = _NO_HASH
        self._last_successful_render = 0.0
        if not session_name:
            try:
                self.query_one("#terminal-content", Static).update(
                    "Select a session · Ctrl+A to attach"
                )
            except Exception:
                logger.debug("terminal-content widget not available during session switch", exc_info=True)
        if session_name:
            # Don't blank the screen — keep stale content visible until the
            # first capture arrives, avoiding the black-screen flash.
            self._poll_pane()  # Initial capture
            self._fallback_timer = self.set_interval(PANE_FALLBACK_POLL_S, self._poll_pane)

    def pause_watching(self) -> None:
        """Stop polling when this worktree tab becomes inactive."""
        if self._fallback_timer is not None:
            self._fallback_timer.stop()
            self._fallback_timer = None

    def resume_watching(self) -> None:
        """Resume polling when this worktree tab becomes active again."""
        if not self.active_session:
            return
        if self._fallback_timer is not None:
            return  # Already running
        self._poll_pane()
        self._fallback_timer = self.set_interval(PANE_FALLBACK_POLL_S, self._poll_pane)

    def start_watching_states(self, session_names: list[str]) -> None:
        """Start watching state files for all given sessions.

        Adds new watches and removes stale ones. Safe to call repeatedly.
        """
        new_set = set(session_names)
        for name in self._watched_state_sessions - new_set:
            self._watcher.stop_watching_state(name)
        for name in new_set - self._watched_state_sessions:
            self._watcher.start_watching_state(name, self._on_state_changed)
        self._watched_state_sessions = new_set

    def _on_state_changed(self, session_name: str) -> None:
        """Called by kqueue watcher when a session's state file changes."""
        try:
            self.post_message(self.StateChanged(session_name))
        except Exception:
            pass

    def _poll_pane(self) -> None:
        session = self.active_session
        if not session:
            return
        import time
        if (time.monotonic() - self._last_successful_render) > _FORCE_REFRESH_S:
            self._last_hash = _NO_HASH
        self.run_worker(lambda: self._capture(session), thread=True, exclusive=True)

    def _capture(self, session_name: str) -> tuple[int, Text] | None:
        raw = capture_pane(session_name)
        content_hash = hash(raw)
        if content_hash == self._last_hash:
            return None
        clean = _BG_ANSI_RE.sub("", raw)
        return content_hash, Text.from_ansi(clean)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state != WorkerState.SUCCESS or event.worker.result is None:
            return
        self._last_hash = event.worker.result[0]
        import time
        self._last_successful_render = time.monotonic()
        try:
            self.query_one("#terminal-content", Static).update(event.worker.result[1])
        except Exception:
            logger.debug("terminal-content widget not available during pane update", exc_info=True)

    # Map Textual key names to tmux special key names
    _SPECIAL_KEY_MAP = {
        "enter": "Enter",
        "return": "Enter",
        "escape": "Escape",
        "backspace": "BSpace",
        "delete": "DC",
        "up": "Up",
        "down": "Down",
        "left": "Left",
        "right": "Right",
        "home": "Home",
        "end": "End",
        "pageup": "PPage",
        "pagedown": "NPage",
    }

    # Key combos that insert a newline in Claude Code's input.
    _NEWLINE_KEYS = {
        "shift+enter", "shift+return",
        "alt+enter", "alt+return",
    }

    def on_unmount(self) -> None:
        if self._fallback_timer is not None:
            self._fallback_timer.stop()
            self._fallback_timer = None
        self._watcher.cleanup()
        self._send_keys_executor.shutdown(wait=False)

    def _send_keys_async(self, *keys: str, literal: bool = False) -> None:
        """Send keys off the event loop, in-order via a single-thread executor."""
        session = self.active_session
        if not session:
            return
        self._send_keys_executor.submit(send_keys, session, *keys, literal=literal)

    def on_click(self, event: Click) -> None:
        """Consume clicks so the Static child doesn't trigger text selection."""
        event.stop()
        self.focus()

    def on_paste(self, event: Paste) -> None:
        if not self.active_session or not event.text:
            return
        event.stop()
        self._send_keys_async(event.text, literal=True)

    def on_key(self, event: Key) -> None:
        if not self.active_session:
            return

        key = event.key
        if key in RESERVED_KEYS:
            return

        event.prevent_default()
        event.stop()

        if key in self._NEWLINE_KEYS:
            # Forward as Alt+Enter (ESC followed by Enter) so Claude Code
            # interprets it as "insert newline" rather than "submit".
            self._send_keys_async("Escape", "Enter")
        elif key in self._SPECIAL_KEY_MAP:
            self._send_keys_async(self._SPECIAL_KEY_MAP[key])
        elif event.character and len(event.character) == 1:
            # Send printable characters as literal text so that '/', ';',
            # and other tmux-special characters arrive unmangled.
            self._send_keys_async(event.character, literal=True)
        elif key.startswith("ctrl+"):
            letter = key.split("+", 1)[1]
            self._send_keys_async(f"C-{letter}")
