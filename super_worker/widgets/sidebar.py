from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Label, ListItem, ListView, Static

from super_worker.constants import get_session_type_tag
from super_worker.models import Session, Worktree
from super_worker.services.tmux import SessionState, batch_detect_session_states
from super_worker.services.worktree import get_branch_status, get_worktree_dirty


class SessionSelected(Message):
    """Fired when a session is selected in the sidebar."""

    def __init__(self, worktree: Worktree, session: Session) -> None:
        self.worktree = worktree
        self.session = session
        super().__init__()


class SessionDeleted(Message):
    """Fired when a session is deleted from the sidebar."""

    def __init__(self, worktree: Worktree, session: Session) -> None:
        self.worktree = worktree
        self.session = session
        super().__init__()


class GitAction(Message):
    """Fired when a git action button is pressed."""

    def __init__(self, worktree: Worktree, action: str) -> None:
        self.worktree = worktree
        self.action = action
        super().__init__()


class SessionSidebar(Vertical):
    """Vertical sidebar showing sessions and git status for the active worktree."""

    DEFAULT_CSS = """
    SessionSidebar {
        width: 32;
        min-width: 16;
        height: 1fr;
        background: $surface;
        padding: 0;
    }
    .sidebar-section {
        height: 1;
        padding: 0 1;
        text-style: bold;
        color: $accent;
    }
    #sidebar-info {
        height: auto;
        padding: 0 1;
        color: $text-muted;
    }
    #session-list {
        height: 1fr;
        min-height: 4;
    }
    #session-list > ListItem.--highlight {
        background: $accent 30%;
    }
    #git-status {
        height: auto;
        padding: 0 1;
        color: $text-muted;
    }
    #sidebar-hint {
        height: auto;
        padding: 0 1;
        color: $text-muted;
        text-style: italic;
    }
    """

    BINDINGS = [
        Binding("x", "delete_session", "Delete Session", show=True),
    ]

    def __init__(self, remote: str = "origin", main_branch: str = "main") -> None:
        super().__init__()
        self._worktree: Worktree | None = None
        self._session_map: dict[int, Session] = {}
        self._prev_session_snapshot: str = ""
        self._prev_git_snapshot: str = ""
        self._remote = remote
        self._main_branch = main_branch

    def compose(self) -> ComposeResult:
        yield Static("Sessions", classes="sidebar-section")
        yield Static("", id="sidebar-info")
        yield ListView(id="session-list")
        yield Static("Git", classes="sidebar-section")
        yield Static("", id="git-status")
        yield Static("x: delete session", id="sidebar-hint")

    @staticmethod
    def _state_dot(state: SessionState) -> str:
        if state == SessionState.DEAD:
            return "[red]●[/]"
        if state == SessionState.WAITING_APPROVAL:
            return "[magenta]●[/]"
        if state == SessionState.WAITING_INPUT:
            return "[yellow]●[/]"
        if state == SessionState.RUNNING:
            return "[green]●[/]"
        return "[dim]●[/]"

    def show_worktree(
        self,
        worktree: Worktree,
        states: dict[str, SessionState] | None = None,
        git_status: dict | None = None,
        git_dirty: bool | None = None,
        refresh_git: bool = True,
    ) -> None:
        is_new_worktree = self._worktree is not worktree
        self._worktree = worktree

        if is_new_worktree:
            info = self.query_one("#sidebar-info", Static)
            info.update(f" path: {worktree.path}")

        # Use pre-fetched states or fetch inline
        if states is None:
            session_names = [s.tmux_session_name for s in worktree.sessions]
            states = batch_detect_session_states(session_names)

        # Build snapshot to detect changes
        snapshot_parts = []
        for s in worktree.sessions:
            state = states.get(s.tmux_session_name, SessionState.RUNNING)
            snapshot_parts.append(f"{s.id}:{s.label}:{s.session_type}:{state.value}")
        snapshot = "|".join(snapshot_parts)

        if snapshot == self._prev_session_snapshot and not is_new_worktree:
            # No change in sessions - skip list rebuild entirely
            if refresh_git:
                self._refresh_git_status(worktree, status=git_status, dirty=git_dirty)
            return

        self._prev_session_snapshot = snapshot
        self._session_map.clear()

        sess_list = self.query_one("#session-list", ListView)
        prev_index = sess_list.index
        current_count = len(sess_list.children)
        new_count = len(worktree.sessions)

        # Update existing items in-place, add/remove only as needed
        for i, s in enumerate(worktree.sessions):
            state = states.get(s.tmux_session_name, SessionState.RUNNING)
            dot = self._state_dot(state)
            tag = f"[dim]{get_session_type_tag(s.session_type)}[/]"
            label_text = f"{dot} {tag} {s.label}"
            self._session_map[i] = s

            if i < current_count:
                # Update existing ListItem's label in-place
                item = sess_list.children[i]
                lbl = item.query_one(Label)
                lbl.update(label_text)
            else:
                # Append new item
                label = Label(label_text)
                label.markup = True
                sess_list.append(ListItem(label))

        # Remove excess items from the end (iterate a snapshot to avoid
        # issues with async removal not shrinking children immediately)
        for child in list(sess_list.children[new_count:]):
            child.remove()

        if prev_index is not None and prev_index < new_count:
            sess_list.index = prev_index

        if refresh_git:
            self._refresh_git_status(worktree, status=git_status, dirty=git_dirty)

    def _refresh_git_status(self, worktree: Worktree, status: dict | None = None, dirty: bool | None = None) -> None:
        if status is None:
            status = get_branch_status(worktree.path, self._remote, self._main_branch)
        if dirty is None:
            dirty = get_worktree_dirty(worktree.path)

        git_snapshot = f"{worktree.branch}:{status['ahead']}:{status['behind']}:{dirty}"
        if git_snapshot == self._prev_git_snapshot:
            return
        self._prev_git_snapshot = git_snapshot

        parts = [f" branch: {worktree.branch}"]
        parts.append(f" ↑ {status['ahead']} ahead  ↓ {status['behind']} behind")
        if dirty:
            parts.append(" [yellow]● uncommitted changes[/]")
        else:
            parts.append(" [green]● clean[/]")

        git_status = self.query_one("#git-status", Static)
        git_status.markup = True
        git_status.update("\n".join(parts))

    def select_session(self, tmux_session_name: str) -> None:
        """Programmatically highlight a session in the list by its tmux name."""
        for idx, session in self._session_map.items():
            if session.tmux_session_name == tmux_session_name:
                try:
                    self.query_one("#session-list", ListView).index = idx
                except Exception:
                    pass
                return

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id != "session-list":
            return
        idx = event.list_view.index
        if idx is not None and idx in self._session_map and self._worktree:
            self.post_message(SessionSelected(self._worktree, self._session_map[idx]))

    def action_delete_session(self) -> None:
        if not self._worktree:
            return
        # Delete the session highlighted in the ListView, not the app's active session
        sess_list = self.query_one("#session-list", ListView)
        idx = sess_list.index
        if idx is not None and idx in self._session_map:
            session = self._session_map[idx]
            self.post_message(SessionDeleted(self._worktree, session))


class SidebarDivider(Static):
    """Thin vertical bar between the session sidebar and the terminal pane.

    Drag it left/right to resize the sidebar. The chosen width is a class-level
    value so every worktree tab (across all projects) stays in sync for the rest
    of the session. Nothing is persisted to disk — reopening the app restores the
    default width.
    """

    DEFAULT_CSS = """
    SidebarDivider {
        width: 1;
        height: 1fr;
        background: $panel;
        color: $accent;
        content-align: center middle;
    }
    SidebarDivider:hover, SidebarDivider.-dragging {
        background: $accent;
    }
    """

    MIN_WIDTH = 16          # never shrink the sidebar below this
    _MIN_TERMINAL = 24      # always leave at least this many columns for the terminal
    _shared_width: int | None = None  # persists across tabs for the session

    def __init__(self) -> None:
        super().__init__("┊")
        self._dragging = False

    def on_mount(self) -> None:
        if SidebarDivider._shared_width is not None:
            self._set_sidebar_width(SidebarDivider._shared_width)

    def _sidebar(self) -> SessionSidebar | None:
        parent = self.parent
        if parent is None:
            return None
        try:
            return parent.query_one(SessionSidebar)
        except Exception:
            return None

    def _clamp(self, width: int) -> int:
        parent = self.parent
        avail = parent.region.width if parent is not None else 0
        upper = max(self.MIN_WIDTH, avail - self._MIN_TERMINAL)
        return max(self.MIN_WIDTH, min(width, upper))

    def _set_sidebar_width(self, width: int) -> None:
        sidebar = self._sidebar()
        if sidebar is not None:
            sidebar.styles.width = width

    def on_mouse_down(self, event) -> None:
        self._dragging = True
        self.add_class("-dragging")
        self.capture_mouse()
        event.stop()

    def on_mouse_move(self, event) -> None:
        if not self._dragging:
            return
        parent = self.parent
        if parent is None:
            return
        width = self._clamp(int(event.screen_x) - parent.region.x)
        self._set_sidebar_width(width)          # live feedback on this tab
        SidebarDivider._shared_width = width
        event.stop()

    def on_mouse_up(self, event) -> None:
        if not self._dragging:
            return
        self._dragging = False
        self.remove_class("-dragging")
        self.release_mouse()
        # Propagate the final width to every other tab's sidebar so they match.
        width = SidebarDivider._shared_width
        if width is not None:
            try:
                for sidebar in self.app.query(SessionSidebar):
                    sidebar.styles.width = width
            except Exception:
                pass
        event.stop()
