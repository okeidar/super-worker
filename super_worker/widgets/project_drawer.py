"""Overlay project switcher drawer + docked project tab bar."""

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.events import Key
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button, Input, Label, ListItem, ListView, Static


# ── Messages ─────────────────────────────────────────────────────────────────


class ProjectSelected(Message):
    """Fired when the user picks a project (drawer or tab bar)."""

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__()


class DockToggled(Message):
    """Fired when the drawer's pin button is pressed.

    docked=True  → hide the drawer, show ProjectTabBar above worktree tabs
    docked=False → hide ProjectTabBar, show drawer as floating overlay
    """

    def __init__(self, docked: bool) -> None:
        self.docked = docked
        super().__init__()


# ── ProjectTabBar (docked mode: horizontal tab strip above worktree tabs) ────


class ProjectTabBar(Widget):
    """Horizontal project tab strip.  Shown instead of the drawer when docked.

    CSS classes:
      .-visible — makes the bar appear (1 line above worktree tabs)
    """

    DEFAULT_CSS = """
    ProjectTabBar {
        height: 1;
        display: none;
        background: $panel;
        border-bottom: solid $accent;
    }
    ProjectTabBar.-visible {
        display: block;
    }
    .proj-tab {
        height: 1;
        padding: 0 1;
        min-width: 6;
        border: none;
        background: $panel;
        color: $text-muted;
    }
    .proj-tab:hover {
        background: $accent 15%;
        color: $text;
    }
    .proj-tab.-active {
        background: $accent 25%;
        color: $text;
        text-style: bold;
    }
    #tab-open-btn {
        width: 5;
        height: 1;
        border: none;
        background: $panel;
        color: $text-muted;
        padding: 0 1;
    }
    #tab-open-btn:hover {
        background: $accent 15%;
    }
    #tab-undock-btn {
        width: 3;
        height: 1;
        border: none;
        background: $panel;
        color: $text-muted;
        dock: right;
    }
    #tab-undock-btn:hover {
        background: $accent 15%;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._open_paths: list[str] = []
        self._current: str | None = None

    def compose(self) -> ComposeResult:
        yield Button("📍", id="tab-undock-btn", tooltip="Undock (show as side panel)")
        yield Horizontal(id="tab-inner")
        yield Button("+ Open", id="tab-open-btn")

    def on_mount(self) -> None:
        self.query_one("#tab-undock-btn", Button).can_focus = False
        self.query_one("#tab-open-btn", Button).can_focus = False

    def show(self) -> None:
        self.add_class("-visible")

    def hide(self) -> None:
        self.remove_class("-visible")

    def refresh_projects(self, open_paths: set[str], current: str | None) -> None:
        self._open_paths = list(open_paths)
        self._current = current
        self.call_after_refresh(self._rebuild_tabs)

    def _rebuild_tabs(self) -> None:
        try:
            inner = self.query_one("#tab-inner", Horizontal)
        except Exception:
            return
        inner.remove_children()
        for path in self._open_paths:
            name = Path(path).name
            is_active = path == self._current
            btn = Button(name, classes="proj-tab" + (" -active" if is_active else ""))
            btn._project_path = path  # type: ignore[attr-defined]
            btn.can_focus = False
            inner.mount(btn)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "tab-undock-btn":
            self.post_message(DockToggled(docked=False))
            return
        if event.button.id == "tab-open-btn":
            # Ask app to open the floating drawer as a picker
            self.post_message(DockToggled(docked=False))
            return
        path = getattr(event.button, "_project_path", None)
        if path:
            self.post_message(ProjectSelected(path))


# ── ProjectDrawer (overlay side panel) ───────────────────────────────────────


class ProjectDrawer(Widget):
    """Left-side project switcher panel.

    Default: hidden.  Press Ctrl+O to slide it in.  Press the 📌 button or
    'p' (while drawer is focused) to dock it — this hides the drawer and
    tells the app to show ProjectTabBar above the worktree tabs instead.

    CSS classes:
      .-open — visible as overlay (Ctrl+O toggle)
    """

    DEFAULT_CSS = """
    ProjectDrawer {
        width: 38;
        min-width: 26;
        height: 1fr;
        background: $surface;
        border-right: solid $accent;
        display: none;
        padding: 0;
    }
    ProjectDrawer.-open {
        display: block;
    }
    #drawer-header {
        height: 1;
        padding: 0 1;
        text-style: bold;
        color: $accent;
        background: $panel;
    }
    #drawer-pin-btn {
        width: auto;
        min-width: 3;
        height: 1;
        border: none;
        background: $panel;
        color: $accent;
        padding: 0 1;
    }
    #project-list {
        height: 1fr;
    }
    .proj-item-label {
        padding: 0 1;
    }
    #drawer-hint {
        height: 1;
        padding: 0 1;
        color: $text-muted;
        text-style: italic;
    }
    #drawer-input-area {
        height: 5;
        padding: 0 1;
        border-top: solid $panel;
    }
    #drawer-input-label {
        height: 1;
        color: $text-muted;
    }
    #drawer-input {
        margin: 0;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._projects: list[str] = []
        self._current: str | None = None
        self._open_paths: set[str] = set()

    def compose(self) -> ComposeResult:
        with Horizontal(id="drawer-header"):
            yield Static("Projects", id="drawer-title")
            yield Button("📌", id="drawer-pin-btn", tooltip="Dock above worktree tabs")
        yield ListView(id="project-list")
        yield Static("↑↓ navigate  Enter: open  p: dock", id="drawer-hint")
        with Vertical(id="drawer-input-area"):
            yield Label("Open path:", id="drawer-input-label")
            yield Input(placeholder="/path/to/repo", id="drawer-input")

    def on_mount(self) -> None:
        self.query_one("#drawer-pin-btn", Button).can_focus = False

    def refresh_projects(
        self,
        projects: list[str],
        current: str | None,
        open_paths: set[str],
    ) -> None:
        """Rebuild the project list. Call whenever open projects change."""
        self._projects = projects
        self._current = current
        self._open_paths = open_paths
        self.call_after_refresh(self._rebuild_list)

    def _rebuild_list(self) -> None:
        try:
            lst = self.query_one("#project-list", ListView)
        except Exception:
            return
        lst.clear()
        for path in self._projects:
            name = Path(path).name
            is_active = path == self._current
            is_open = path in self._open_paths

            if is_active:
                marker = "[bold green]▶[/] "
            elif is_open:
                marker = "[green]○[/] "
            else:
                marker = "  "

            label = Label(f"{marker}{name}", classes="proj-item-label")
            label.markup = True
            item = ListItem(label)
            item._project_path = path  # type: ignore[attr-defined]
            lst.append(item)

    # ── Visibility ────────────────────────────────────────────────────────────

    def open(self) -> None:
        """Show the drawer as a floating overlay."""
        self.add_class("-open")
        self._focus_list()

    def close(self) -> None:
        """Hide the drawer."""
        self.remove_class("-open")

    def toggle(self) -> None:
        """Toggle overlay visibility."""
        if self.has_class("-open"):
            self.close()
        else:
            self.open()

    # ── Dock ──────────────────────────────────────────────────────────────────

    def _request_dock(self) -> None:
        """Pin the drawer: close it and emit DockToggled(docked=True)."""
        self.close()
        self.post_message(DockToggled(docked=True))

    # ── Selection ─────────────────────────────────────────────────────────────

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id != "project-list":
            return
        path = getattr(event.item, "_project_path", None)
        if path:
            self._select_project(path)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        path = event.value.strip()
        if path:
            self._select_project(path)
            self.query_one("#drawer-input", Input).clear()

    def _select_project(self, path: str) -> None:
        self.post_message(ProjectSelected(path))
        self.close()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "drawer-pin-btn":
            self._request_dock()

    def _focus_list(self) -> None:
        try:
            self.query_one("#project-list", ListView).focus()
        except Exception:
            pass

    def on_key(self, event: Key) -> None:
        if event.key == "escape":
            self.close()
            event.stop()
        elif event.key == "p":
            # 'p' only fires here when the drawer (or a child) has focus —
            # never bleeds into the terminal pane which is a sibling widget.
            self._request_dock()
            event.stop()
        elif event.key == "enter":
            lst = self.query_one("#project-list", ListView)
            if lst == self.focused or (self.focused and lst in self.focused.ancestors):
                if lst.index is not None and lst.index < len(self._projects):
                    self._select_project(self._projects[lst.index])
                    event.stop()
