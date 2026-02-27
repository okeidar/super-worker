from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Button, Label, ListItem, ListView, Static

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
        min-width: 26;
        height: 1fr;
        border-right: solid $accent;
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
    #git-status {
        height: auto;
        padding: 0 1;
        color: $text-muted;
    }
    #git-actions {
        height: auto;
        padding: 0 1;
    }
    #git-actions Button {
        width: 100%;
        min-width: 12;
        margin: 0 0 0 0;
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
        with Vertical(id="git-actions"):
            yield Button("Commit", id="btn-git-commit", variant="default")
            yield Button("Push", id="btn-git-push", variant="default")
            yield Button("Pull", id="btn-git-pull", variant="default")
            yield Button("Open PR", id="btn-git-pr", variant="primary")
        yield Static("x: delete session", id="sidebar-hint")

    def on_mount(self) -> None:
        for btn in self.query("#git-actions Button"):
            btn.can_focus = False

    @staticmethod
    def _state_dot(state: SessionState) -> str:
        if state == SessionState.DEAD:
            return "[red]●[/]"
        if state == SessionState.WAITING_APPROVAL:
            return "[magenta]●[/]"
        if state == SessionState.WAITING_INPUT:
            return "[yellow]●[/]"
        return "[green]●[/]"

    def show_worktree(
        self,
        worktree: Worktree,
        states: dict[str, SessionState] | None = None,
        git_status: dict | None = None,
        git_dirty: bool | None = None,
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
            snapshot_parts.append(f"{s.id}:{s.label}:{state.value}")
        snapshot = "|".join(snapshot_parts)

        if snapshot == self._prev_session_snapshot and not is_new_worktree:
            # No change in sessions - skip list rebuild entirely
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
            label_text = f"{dot} {s.label}"
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

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id != "session-list":
            return
        idx = event.list_view.index
        if idx is not None and idx in self._session_map and self._worktree:
            self.post_message(SessionSelected(self._worktree, self._session_map[idx]))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if not self._worktree:
            return
        btn_id = event.button.id
        if btn_id == "btn-git-commit":
            self.post_message(GitAction(self._worktree, "commit"))
        elif btn_id == "btn-git-push":
            self.post_message(GitAction(self._worktree, "push"))
        elif btn_id == "btn-git-pull":
            self.post_message(GitAction(self._worktree, "pull"))
        elif btn_id == "btn-git-pr":
            self.post_message(GitAction(self._worktree, "pr"))

    def action_delete_session(self) -> None:
        if not self._worktree:
            return
        # Find the active session from the app's state
        active_name = self.app._active_session_name  # type: ignore[attr-defined]
        if active_name:
            session = next((s for s in self._worktree.sessions if s.tmux_session_name == active_name), None)
            if session:
                self.post_message(SessionDeleted(self._worktree, session))
