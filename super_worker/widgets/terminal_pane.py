import concurrent.futures
import logging
import time
from dataclasses import dataclass, field

from rich.cells import cell_len
from rich.text import Text
from textual.events import Click, Key, MouseScrollDown, MouseScrollUp, Paste, Resize
from textual.message import Message
from textual.reactive import reactive
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Static
from textual.worker import Worker, WorkerState

from super_worker.constants import (
    PANE_ECHO_POLL_S,
    PANE_FALLBACK_POLL_S,
    PANE_HISTORY_CHUNK_LINES,
    PANE_HISTORY_MAX_LINES,
    RESERVED_KEYS,
)
from super_worker.services.pane_watcher import PaneWatcher
from super_worker.services.tmux import (
    PaneSnapshot,
    capture_history_tail,
    capture_pane_snapshot,
    paste_to_pane,
    resize_window,
    send_keys,
    set_window_size,
)

logger = logging.getLogger(__name__)

# Sentinel that never equals any real render key — avoids the hash("") == 0 bug
_NO_KEY = object()

# If no successful render for this long, force a re-capture (empty screen recovery)
_FORCE_REFRESH_S = 3.0


def _char_index_for_cell(plain: str, cell_col: int) -> int:
    """Map a terminal cell column to a character index (wide chars are 2 cells)."""
    width = 0
    for i, ch in enumerate(plain):
        if width >= cell_col:
            return i
        width += cell_len(ch)
    return len(plain)


def _apply_cursor(text: Text, line_idx: int, col: int) -> Text:
    """Overlay a reverse-video cursor cell at (line_idx, cell col) in ``text``.

    tmux trims trailing blanks from captures, so the cursor routinely sits
    past the end of its line (e.g. Claude Code's ``❯ `` input row) or below
    the last captured line entirely. Pad lines/columns as needed — the
    snapshot is visible-screen-only, so line_idx is always within the pane.
    """
    if line_idx < 0:
        return text
    lines = text.split("\n", include_separator=False, allow_blank=True)
    while line_idx >= len(lines):
        lines.append(Text(""))
    line = lines[line_idx]
    line_cells = cell_len(line.plain)
    if col >= line_cells:
        line.pad_right(col - line_cells + 1)
    idx = _char_index_for_cell(line.plain, col)
    line.stylize("reverse", idx, idx + 1)
    return Text("\n").join(lines)


def render_snapshot(snapshot: PaneSnapshot) -> Text:
    """Turn a captured pane snapshot into styled Text with a cursor overlay.

    Backgrounds are preserved (not stripped) so diff views and other
    background-colored output render exactly as in a real attach.

    Snapshots are visible-screen-only (history is accumulated separately),
    so the cursor's line is simply ``cursor_y``. Trailing blank lines may be
    trimmed from the capture; ``_apply_cursor`` pads as needed.
    """
    text = Text.from_ansi(snapshot.text)
    if snapshot.cursor_visible and snapshot.pane_height > 0:
        text = _apply_cursor(text, snapshot.cursor_y, snapshot.cursor_x)
    return text


# History drain tuning: each poll captures the estimated-new lines plus this
# overlap, then aligns on the last consumed lines. Alignment is content-based
# because tmux's history_size DECREASES when the buffer evicts old lines at
# its history-limit — a counter-only scheme misreads eviction as a restart.
_HIST_SIG_LINES = 5
_HIST_OVERLAP = 40
_HIST_MIN_WINDOW = 100


@dataclass
class _SessionHistory:
    """Locally accumulated scrollback for one tmux session.

    ``chunks`` hold parsed Text — each history line is parsed exactly once and
    chunks seal at ~PANE_HISTORY_CHUNK_LINES, so old chunks are never
    re-rendered. ``tail_sig`` is the plain text of the last consumed history
    lines, used to align each new drain window; ``last_hist`` is tmux's
    history_size at the last successful drain (a growth *estimate* only).
    """

    chunks: list[Text] = field(default_factory=list)
    counts: list[int] = field(default_factory=list)
    total: int = 0
    tail_sig: str = ""
    last_hist: int = 0
    # Direct references to the mounted Static per chunk (parallel to `chunks`),
    # valid while this session is the one displayed. Holding refs avoids
    # depending on scroll.query() reflecting Textual's deferred mount/remove.
    widgets: list = field(default_factory=list)


def _find_suffix(haystack: list[str], needle: list[str]) -> int | None:
    """Index of the LAST occurrence of ``needle`` as a sublist of ``haystack``."""
    if not needle or len(needle) > len(haystack):
        return None
    for i in range(len(haystack) - len(needle), -1, -1):
        if haystack[i:i + len(needle)] == needle:
            return i
    return None


class _PaneScroll(VerticalScroll):
    """Scroll container that hands the wheel to the pane's application.

    When the program in the tmux pane requested mouse reporting (Claude Code,
    vim, less, …), wheel events are forwarded into the pane as SGR mouse
    sequences — the app scrolls its own content, exactly like a real attach.
    Otherwise the wheel scrolls this container's local scrollback as usual.
    """

    forward_wheel = None  # set by TerminalPane: callable(up: bool, event) -> bool

    def _on_mouse_scroll_up(self, event: MouseScrollUp) -> None:
        if self.forward_wheel and self.forward_wheel(True, event):
            event.stop()
            event.prevent_default()
            return
        super()._on_mouse_scroll_up(event)

    def _on_mouse_scroll_down(self, event: MouseScrollDown) -> None:
        if self.forward_wheel and self.forward_wheel(False, event):
            event.stop()
            event.prevent_default()
            return
        super()._on_mouse_scroll_down(event)


class TerminalPane(Widget, can_focus=True):
    """Displays captured tmux pane content and forwards keystrokes.

    Aims to be interactive as if attached: full color (incl. backgrounds),
    a live cursor, follow-tail scrolling, and the tmux window resized to match
    this widget. Press Ctrl+A for a true attach (mouse, alt-screen apps).
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
        /* Reserve border space permanently: if the border only existed on
           :focus, focusing would shrink the content by 2 cells each way and
           re-trigger the tmux resize — the window would thrash between two
           sizes on every focus change. */
        border: tall $panel;
    }
    TerminalPane:focus {
        border: tall $accent;
    }
    #terminal-scroll {
        width: 1fr;
        height: 1fr;
        overflow-y: auto;
        /* Reserve the scrollbar column permanently. Otherwise the scrollbar
           appearing/disappearing changes content width, which re-triggers the
           tmux resize, which reflows content — an oscillation loop. */
        scrollbar-gutter: stable;
    }
    #terminal-content {
        width: 1fr;
        height: auto;
    }
    .hist-chunk {
        width: 1fr;
        height: auto;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._last_key: object = _NO_KEY
        self._fallback_timer = None
        self._resize_timer = None
        self._last_successful_render: float = 0.0
        self._last_size: tuple[int, int] | None = None
        self._echo_pending = False  # throttle: at most one armed echo poll
        self._paused = False
        # True when the pane's app requested mouse reporting (from the last
        # snapshot) — wheel/clicks are then forwarded into the pane.
        self._mouse_app = False
        # Locally accumulated scrollback, keyed by session. History lines are
        # immutable once pushed, so each is captured and parsed exactly once.
        self._hist: dict[str, _SessionHistory] = {}
        self._watcher = PaneWatcher()
        self._watched_state_sessions: set[str] = set()
        # Single-thread executor guarantees FIFO key delivery.
        # run_worker without exclusive=True spawns concurrent workers that can
        # race each other, causing characters to arrive at tmux out of order.
        self._send_keys_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="sw-send-keys"
        )

    def compose(self) -> ComposeResult:
        scroll = _PaneScroll(id="terminal-scroll")
        scroll.forward_wheel = self._forward_wheel
        with scroll:
            content = Static("Select a session · Ctrl+A to attach", id="terminal-content")
            content.auto_links = False
            content.ALLOW_SELECT = False
            yield content

    def watch_active_session(self, old_value: str | None, session_name: str | None) -> None:
        if self._fallback_timer is not None:
            self._fallback_timer.stop()
            self._fallback_timer = None
        self._last_key = _NO_KEY
        self._last_successful_render = 0.0
        self._last_size = None  # Force a resize for the newly-shown session
        self._remount_history(session_name)
        if not session_name:
            try:
                self.query_one("#terminal-content", Static).update(
                    "Select a session · Ctrl+A to attach"
                )
            except Exception:
                logger.debug("terminal-content widget not available during session switch", exc_info=True)
        if session_name:
            self._paused = False
            # Don't blank the screen — keep stale content visible until the
            # first capture arrives, avoiding the black-screen flash.
            self._poll_pane()  # Initial capture
            self.call_after_refresh(self._apply_tmux_size)
            self._fallback_timer = self.set_interval(PANE_FALLBACK_POLL_S, self._poll_pane)

    def pause_watching(self) -> None:
        """Stop polling when this worktree tab becomes inactive."""
        self._paused = True
        if self._fallback_timer is not None:
            self._fallback_timer.stop()
            self._fallback_timer = None

    def resume_watching(self) -> None:
        """Resume polling when this worktree tab becomes active again."""
        self._paused = False
        if not self.active_session:
            return
        self._last_size = None  # Re-apply size now that the pane is visible again
        self.call_after_refresh(self._apply_tmux_size)
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
            self._hist.pop(name, None)  # Drop scrollback cache for removed sessions
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
        if (time.monotonic() - self._last_successful_render) > _FORCE_REFRESH_S:
            self._last_key = _NO_KEY
        self.run_worker(lambda: self._capture(session), thread=True, exclusive=True)

    def _hist_state(self, session_name: str) -> _SessionHistory:
        state = self._hist.get(session_name)
        if state is None:
            state = self._hist[session_name] = _SessionHistory()
        return state

    def _capture(
        self, session_name: str
    ) -> tuple[str, object, Text, Text | None, str | None, int, bool] | None:
        """Crash-proof wrapper around the capture body.

        Runs every ~150ms in a worker with exit_on_error=True — any unhandled
        exception here would take the whole app down in a tight loop. Swallow
        and skip this frame instead; the next poll retries.
        """
        try:
            return self._capture_impl(session_name)
        except Exception:
            logger.debug("capture failed (skipping frame)", exc_info=True)
            return None

    def _capture_impl(
        self, session_name: str
    ) -> tuple[str, object, Text, Text | None, str | None, int, bool] | None:
        """Worker thread: capture visible screen + drain new history lines.

        Returns (session, render key, live text, new history Text or None,
        new tail signature or None, history_size, clear_first flag). History
        lines are parsed here, off the UI thread, exactly once each.

        Draining aligns on content (``tail_sig``), not on history_size
        arithmetic: once tmux's buffer hits history-limit it evicts old lines
        and the counter shrinks, which naive delta logic misreads as a
        session restart.
        """
        snapshot = capture_pane_snapshot(session_name)
        state = self._hist_state(session_name)
        hist = snapshot.history_size

        hist_text: Text | None = None
        new_sig: str | None = None
        clear_first = False
        if hist > 0:
            full_window = min(hist, PANE_HISTORY_MAX_LINES)
            if state.total == 0 and not state.tail_sig:
                window = full_window  # bootstrap: take everything available
            else:
                # Growth estimate + overlap. Under eviction (buffer at
                # history-limit) the counter stays flat while lines churn, so
                # this can undershoot — the full-window retry below recovers.
                grown = max(hist - state.last_hist, 0)
                window = min(max(grown + _HIST_OVERLAP, _HIST_MIN_WINDOW), full_window)

            parsed_lines: list[Text] = []
            plain_lines: list[str] = []
            start = 0
            for attempt in dict.fromkeys((window, full_window)):  # dedup, keep order
                raw = capture_history_tail(session_name, attempt)
                if not raw:
                    parsed_lines = []
                    break
                parsed_lines = Text.from_ansi(raw).split(
                    "\n", include_separator=False, allow_blank=True
                )
                plain_lines = [ln.plain for ln in parsed_lines]
                if not state.tail_sig:
                    break
                idx = _find_suffix(plain_lines, state.tail_sig.split("\n"))
                if idx is not None:
                    start = idx + len(state.tail_sig.split("\n"))
                    break
                if attempt >= full_window:
                    # No alignment even against the whole buffer.
                    if hist < _HIST_MIN_WINDOW and state.total > hist:
                        # Tiny history: pane restarted (respawn/clear) —
                        # drop stale scrollback.
                        clear_first = True
                    # else: burst outran the tmux buffer — append the whole
                    # window; the unreachable gap is lost, never duplicated.

            if parsed_lines:
                if start < len(parsed_lines):
                    hist_text = Text("\n").join(parsed_lines[start:])
                new_sig = "\n".join(plain_lines[-_HIST_SIG_LINES:])

        key = (
            hash(snapshot.text),
            snapshot.cursor_x,
            snapshot.cursor_y,
            snapshot.cursor_visible,
            hist,
        )
        if key == self._last_key and hist_text is None and not clear_first:
            # Still keep the mouse flag fresh — apps toggle mouse reporting
            # (e.g. entering/leaving less) without changing screen content.
            self._mouse_app = snapshot.mouse_any
            return None
        return (session_name, key, render_snapshot(snapshot), hist_text,
                new_sig, hist, clear_first, snapshot.mouse_any)

    def _at_bottom(self) -> bool:
        try:
            scroll = self.query_one("#terminal-scroll", VerticalScroll)
            return scroll.scroll_offset.y >= scroll.max_scroll_y - 1
        except Exception:
            return True

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state != WorkerState.SUCCESS or event.worker.result is None:
            return
        session_name, key, live_text, hist_text, new_sig, hist_size, clear_first, mouse_any = event.worker.result
        if session_name != self.active_session:
            return  # Stale capture from before a session switch
        self._mouse_app = mouse_any
        # Scrolled up = reading history: freeze the view — don't mutate
        # content and don't consume history (alignment is content-based, so
        # it drains cleanly after unfreezing). Polling continues; the first
        # capture after returning to the bottom renders everything pending.
        if not self._at_bottom():
            self._last_key = _NO_KEY
            return

        state = self._hist_state(session_name)
        if clear_first:
            state.chunks.clear()
            state.counts.clear()
            state.total = 0
            self._remount_history(session_name)
        if hist_text is not None:
            self._append_history(session_name, hist_text)
        if new_sig is not None:
            state.tail_sig = new_sig
        state.last_hist = hist_size

        self._last_key = key
        self._last_successful_render = time.monotonic()
        try:
            self.query_one("#terminal-content", Static).update(live_text)
        except Exception:
            logger.debug("terminal-content widget not available during pane update", exc_info=True)
            return
        # Follow the live tail (we're at the bottom — checked above).
        self.call_after_refresh(self._scroll_to_end)

    def _scroll_to_end(self) -> None:
        try:
            self.query_one("#terminal-scroll", VerticalScroll).scroll_end(animate=False)
        except Exception:
            pass

    # ── Local scrollback (parse-once history chunks) ─────────────────────────

    def _append_history(self, session_name: str, new_text: Text) -> None:
        """Fold newly drained history lines into per-session chunks and the DOM.

        The newest (open) chunk absorbs lines until it reaches the chunk size,
        then is sealed — sealed chunks and their Static widgets are never
        touched again, so render cost stays constant as scrollback grows.

        Widget refs are tracked in state.widgets (parallel to state.chunks),
        so the DOM mirror never depends on scroll.query() timing. Callers only
        append for the currently-displayed session.
        """
        state = self._hist_state(session_name)
        new_lines = new_text.plain.count("\n") + 1

        try:
            scroll = self.query_one("#terminal-scroll", VerticalScroll)
            content = self.query_one("#terminal-content", Static)
        except Exception:
            return  # Not composed — skip; drain retries next poll

        merged_into_open = bool(state.counts) and state.counts[-1] < PANE_HISTORY_CHUNK_LINES
        if merged_into_open:
            state.chunks[-1] = Text("\n").join([state.chunks[-1], new_text])
            state.counts[-1] += new_lines
            if state.widgets:
                state.widgets[-1].update(state.chunks[-1])
        else:
            state.chunks.append(new_text)
            state.counts.append(new_lines)
            widget = Static(new_text, classes="hist-chunk")
            widget.ALLOW_SELECT = False
            state.widgets.append(widget)
            scroll.mount(widget, before=content)
        state.total += new_lines

        # Prune oldest sealed chunks + their widgets in lockstep.
        while state.total > PANE_HISTORY_MAX_LINES and len(state.chunks) > 1:
            state.total -= state.counts.pop(0)
            state.chunks.pop(0)
            if state.widgets:
                old = state.widgets.pop(0)
                try:
                    old.remove()
                except Exception:
                    pass

    def _remount_history(self, session_name: str | None) -> None:
        """Replace mounted history chunks with the given session's cache."""
        try:
            scroll = self.query_one("#terminal-scroll", VerticalScroll)
            content = self.query_one("#terminal-content", Static)
        except Exception:
            return  # Not composed yet — nothing mounted, nothing to swap
        # Remove whatever is currently mounted (from any prior session) and
        # clear its widget refs — those widgets are being detached.
        for w in scroll.query(".hist-chunk"):
            w.remove()
        for st in self._hist.values():
            st.widgets = []
        if not session_name:
            return
        state = self._hist.get(session_name)
        if not state:
            return
        for chunk in state.chunks:
            widget = Static(chunk, classes="hist-chunk")
            widget.ALLOW_SELECT = False
            state.widgets.append(widget)
            scroll.mount(widget, before=content)

    # ── Sizing ──────────────────────────────────────────────────────────────

    def on_resize(self, event: Resize) -> None:
        self._apply_tmux_size()

    def _apply_tmux_size(self) -> None:
        """Schedule a debounced tmux resize to the visible content region.

        Debounced because resize events arrive in bursts (initial layout,
        window drags): resizing tmux on each intermediate size makes the
        session reflow repeatedly and can persist a transient size if the
        pane gets hidden mid-layout.
        """
        if self._resize_timer is not None:
            self._resize_timer.stop()
        self._resize_timer = self.set_timer(0.2, self._do_apply_tmux_size)

    def _do_apply_tmux_size(self) -> None:
        """Resize the tmux window to the (now settled) content region.

        Hidden panes report a zero-size content region, so background tabs and
        inactive projects skip resizing automatically.
        """
        self._resize_timer = None
        session = self.active_session
        if not session or self._paused:
            return
        try:
            size = self.query_one("#terminal-scroll", VerticalScroll).content_size
        except Exception:
            return
        w, h = size.width, size.height
        if w <= 0 or h <= 0 or (w, h) == self._last_size:
            return
        self._last_size = (w, h)

        def _do() -> None:
            set_window_size(session, "manual")
            resize_window(session, w, h)

        self._send_keys_executor.submit(_do)

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
        "tab": "Tab",
        "shift+tab": "BTab",
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
        if self._resize_timer is not None:
            self._resize_timer.stop()
            self._resize_timer = None
        self._watcher.cleanup()
        self._send_keys_executor.shutdown(wait=False)

    def _send_keys_async(self, *keys: str, literal: bool = False) -> None:
        """Send keys off the event loop, in-order via a single-thread executor."""
        session = self.active_session
        if not session:
            return
        self._send_keys_executor.submit(send_keys, session, *keys, literal=literal)
        # Typing jumps back to the live tail (standard terminal behavior) —
        # it also unfreezes the scrolled-up view so the echo is visible.
        if not self._at_bottom():
            self._scroll_to_end()
        self._schedule_echo_poll()

    def _schedule_echo_poll(self) -> None:
        """Re-capture shortly after input so the echo shows fast.

        Throttled (not debounced): during a fast burst only ONE echo poll is
        armed at a time, so we don't spawn a timer + capture worker per
        keystroke. It still fires ~PANE_ECHO_POLL_S after the burst starts, so
        the echo stays snappy; the 150ms fallback covers continuous typing.
        """
        if self._echo_pending:
            return
        self._echo_pending = True

        def _fire() -> None:
            self._echo_pending = False
            self._poll_pane()

        self.set_timer(PANE_ECHO_POLL_S, _fire)

    # ── Mouse passthrough (hosted-window behavior) ───────────────────────────

    def _pane_cell(self, event) -> tuple[int, int]:
        """Map a mouse event to 1-based (col, row) cell coords in the pane."""
        col = row = 1
        try:
            region = self.query_one("#terminal-scroll", VerticalScroll).content_region
            col = max(1, event.screen_x - region.x + 1)
            row = max(1, event.screen_y - region.y + 1)
        except Exception:
            pass
        return col, row

    def _forward_wheel(self, up: bool, event) -> bool:
        """Send a wheel event into the pane when its app wants the mouse.

        Claude Code (and vim/less/…) scroll their own content this way —
        the preview behaves like a hosted window of the real app. Returns
        False when the app doesn't do mouse, so the caller falls back to
        scrolling the local scrollback container.
        """
        if not self._mouse_app or not self.active_session:
            return False
        col, row = self._pane_cell(event)
        button = 64 if up else 65  # SGR wheel codes
        self._send_keys_async(f"\x1b[<{button};{col};{row}M", literal=True)
        return True

    def on_mouse_scroll_up(self, event: MouseScrollUp) -> None:
        # Wheel over the pane's padding/border (outside the scroll container).
        if self._forward_wheel(True, event):
            event.stop()

    def on_mouse_scroll_down(self, event: MouseScrollDown) -> None:
        if self._forward_wheel(False, event):
            event.stop()

    def on_click(self, event: Click) -> None:
        """Focus the pane; forward the click when the pane's app is mouse-aware."""
        event.stop()
        self.focus()
        if self._mouse_app and self.active_session:
            col, row = self._pane_cell(event)
            # SGR press + release, left button
            self._send_keys_async(
                f"\x1b[<0;{col};{row}M\x1b[<0;{col};{row}m", literal=True
            )

    def on_paste(self, event: Paste) -> None:
        if not self.active_session or not event.text:
            return
        event.stop()
        session = self.active_session
        # Route through the same single-thread executor as keystrokes so paste
        # stays ordered relative to typing; bracketed paste avoids per-line
        # submission of multi-line text.
        self._send_keys_executor.submit(paste_to_pane, session, event.text)
        self._schedule_echo_poll()

    def on_key(self, event: Key) -> None:
        if not self.active_session:
            return

        key = event.key
        if key in RESERVED_KEYS:
            return

        event.prevent_default()
        event.stop()

        # Shift+PageUp/PageDown scroll the preview itself (classic terminal-
        # emulator behavior); plain PageUp/PageDown go to the session.
        if key in ("shift+pageup", "shift+pagedown"):
            try:
                scroll = self.query_one("#terminal-scroll", VerticalScroll)
                if key == "shift+pageup":
                    scroll.scroll_page_up(animate=False)
                else:
                    scroll.scroll_page_down(animate=False)
            except Exception:
                pass
            return

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
