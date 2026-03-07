"""Overlay project switcher drawer + docked project tab bar."""

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.events import Click, Key
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
    """Fired when the user pins or unpins the project drawer.

    docked=True  → hide the side drawer, show ProjectTabBar above worktree tabs
    docked=False → hide ProjectTabBar, re-open drawer as floating overlay
    """

    def __init__(self, docked: bool) -> None:
        self.docked = docked
        super().__init__()


class ProjectRemoved(Message):
    """Fired when the user removes a project from the registry."""

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__()


# ── _ProjectTab ───────────────────────────────────────────────────────────────


class _TabClose(Static):
    """The × remove button on a project tab."""

    def __init__(self, path: str) -> None:
        super().__init__("×", classes="tab-close")
        self._project_path = path

    def on_click(self, event: Click) -> None:
        event.stop()
        self.post_message(ProjectRemoved(self._project_path))


class _ProjectTab(Horizontal):
    """A clickable project tab with a × remove button.

    Horizontal container so name + × sit side-by-side at height:1.
    """

    def __init__(self, name: str, path: str, active: bool = False, loaded: bool = False) -> None:
        classes = "proj-tab"
        if active:
            classes += " -active"
        elif not loaded:
            classes += " -unloaded"
        super().__init__(classes=classes)
        self._project_path = path
        self._tab_name = name

    def compose(self) -> ComposeResult:
        yield Static(self._tab_name, classes="tab-name")
        yield _TabClose(self._project_path)

    def on_click(self, event: Click) -> None:
        # _TabClose stops its own click; this fires for name-area clicks.
        self.post_message(ProjectSelected(self._project_path))


# ── ProjectTabBar ─────────────────────────────────────────────────────────────


class ProjectTabBar(Widget):
    """One-line horizontal tab strip shown above worktree tabs when docked.

    Layout is explicitly horizontal (Widget default is vertical, which would
    stack children and only show the first one in height:1).

    CSS classes:
      .-visible — makes the bar appear
    """

    DEFAULT_CSS = """
    ProjectTabBar {
        height: 1;
        display: none;
        background: $panel;
        layout: horizontal;
    }
    ProjectTabBar.-visible {
        display: block;
    }
    .proj-tab {
        height: 1;
        width: auto;
        padding: 0 0 0 2;
        background: $panel;
        color: $text;
    }
    .proj-tab.-unloaded {
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
    .tab-name {
        height: 1;
        width: auto;
        padding: 0 1 0 0;
    }
    .tab-close {
        height: 1;
        width: 2;
        color: $text-muted;
        padding: 0;
    }
    .tab-close:hover {
        color: $error;
    }
    #tab-open-btn {
        height: 1;
        width: 8;
        border: none;
        background: $panel;
        color: $text-muted;
        padding: 0 1;
    }
    #tab-open-btn:hover {
        background: $accent 15%;
    }
    #tab-undock-btn {
        height: 1;
        width: 4;
        border: none;
        background: $panel;
        color: $text-muted;
        dock: right;
    }
    #tab-undock-btn:hover {
        color: $accent;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._all_projects: list[str] = []
        self._open_paths: set[str] = set()
        self._current: str | None = None

    def compose(self) -> ComposeResult:
        # Project tabs are mounted dynamically before #tab-open-btn.
        # #tab-undock-btn is docked to the right edge via CSS.
        yield Button("+ Open", id="tab-open-btn")
        yield Button("📍", id="tab-undock-btn", tooltip="Undock to side panel")

    def on_mount(self) -> None:
        self.query_one("#tab-undock-btn", Button).can_focus = False
        self.query_one("#tab-open-btn", Button).can_focus = False

    # ── Visibility ────────────────────────────────────────────────────────────

    def show(self) -> None:
        self.add_class("-visible")
        self._rebuild_tabs()

    def hide(self) -> None:
        self.remove_class("-visible")

    # ── Project list ──────────────────────────────────────────────────────────

    def refresh_projects(
        self,
        all_projects: list[str],
        open_paths: set[str],
        current: str | None,
    ) -> None:
        self._all_projects = all_projects
        self._open_paths = open_paths
        self._current = current
        if self.has_class("-visible"):
            self._rebuild_tabs()

    def _rebuild_tabs(self) -> None:
        try:
            add_btn = self.query_one("#tab-open-btn", Button)
        except Exception:
            return  # widget not yet mounted

        # Remove all existing project tabs in one batch, mount new ones.
        for tab in list(self.query(".proj-tab")):
            tab.remove()

        new_tabs = [
            _ProjectTab(
                Path(path).name,
                path,
                active=path == self._current,
                loaded=path in self._open_paths,
            )
            for path in self._all_projects
        ]
        if new_tabs:
            self.mount(*new_tabs, before=add_btn)

    # ── Events ────────────────────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "tab-undock-btn":
            self.post_message(DockToggled(docked=False))
        elif event.button.id == "tab-open-btn":
            # Open the floating drawer as an overlay picker WITHOUT undocking.
            try:
                from super_worker.widgets.project_drawer import ProjectDrawer  # noqa: F401
                self.app.query_one("#project-drawer").add_class("-open")  # type: ignore[union-attr]
            except Exception:
                pass
        else:
            path = getattr(event.button, "_project_path", None)
            if path:
                self.post_message(ProjectSelected(path))


# ── ProjectDrawer ─────────────────────────────────────────────────────────────


class ProjectDrawer(Widget):
    """Left-side floating project switcher panel.

    Default: hidden.
    Ctrl+O: toggle open/closed as an overlay (side panel).
    📌 button or 'p' (while drawer focused): dock → hides this panel and
    tells the app to show ProjectTabBar above the worktree tabs instead.

    CSS classes:
      .-open — visible as left-side overlay
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
    #drawer-title {
        width: 1fr;
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
    #drawer-pin-btn:hover {
        color: $text;
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
        yield Static("↑↓ ↵:open  Del:remove  p:dock  Esc", id="drawer-hint")
        with Vertical(id="drawer-input-area"):
            yield Label("Open path:", id="drawer-input-label")
            yield Input(placeholder="/path/to/repo", id="drawer-input")

    def on_mount(self) -> None:
        self.query_one("#drawer-pin-btn", Button).can_focus = False

    # ── Project list ──────────────────────────────────────────────────────────

    def refresh_projects(
        self,
        projects: list[str],
        current: str | None,
        open_paths: set[str],
    ) -> None:
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
        """Show as left-side overlay."""
        self.add_class("-open")
        self._focus_list()

    def close(self) -> None:
        """Hide the drawer."""
        self.remove_class("-open")

    def toggle(self) -> None:
        if self.has_class("-open"):
            self.close()
        else:
            self.open()

    # ── Dock ──────────────────────────────────────────────────────────────────

    def _request_dock(self) -> None:
        """Close drawer and ask app to show the tab bar instead."""
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
            # Only fires when the drawer (or a child) has focus — never the terminal.
            self._request_dock()
            event.stop()
        elif event.key == "enter":
            lst = self.query_one("#project-list", ListView)
            # Only handle Enter if the list view is the focused widget
            if self.app.focused == lst or (self.app.focused and lst in self.app.focused.ancestors):
                if lst.index is not None and lst.index < len(self._projects):
                    self._select_project(self._projects[lst.index])
                    event.stop()
        elif event.key == "delete":
            lst = self.query_one("#project-list", ListView)
            if self.app.focused == lst or (self.app.focused and lst in self.app.focused.ancestors):
                if lst.index is not None and lst.index < len(self._projects):
                    path = self._projects[lst.index]
                    self.post_message(ProjectRemoved(path))
                    event.stop()
