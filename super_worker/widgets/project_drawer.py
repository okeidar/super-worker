"""Overlay/dockable project switcher drawer."""

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.events import Key
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button, Input, Label, ListItem, ListView, Static


class ProjectSelected(Message):
    """Fired when the user picks a project in the drawer."""

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__()


class ProjectDrawer(Widget):
    """Side panel listing known projects with open/dock toggle.

    Default: hidden. Press Ctrl+O to show as an overlay (takes layout space
    from the left, content shifts right). Press the pin button or 'p' to dock
    it permanently.

    CSS classes:
      .-open   — visible (overlay mode, toggled by Ctrl+O)
      .-docked — always visible (pinned)
    """

    BINDINGS = [
        Binding("escape", "close", "Close", show=False),
        Binding("p", "toggle_dock", "Pin/Unpin", show=False),
        Binding("enter", "open_selected", "Open", show=False),
    ]

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
    ProjectDrawer.-docked {
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
    #drawer-pin-btn:focus {
        background: $accent 20%;
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
            yield Button("📌", id="drawer-pin-btn")
        yield ListView(id="project-list")
        yield Static("↑↓ navigate  Enter: open  p: pin", id="drawer-hint")
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
        self._rebuild_list()

    def _rebuild_list(self) -> None:
        lst = self.query_one("#project-list", ListView)
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

    # ── Visibility / dock ─────────────────────────────────────────────────────

    def open(self) -> None:
        """Show the drawer (overlay mode)."""
        if not self.has_class("-docked"):
            self.add_class("-open")
        self._focus_list()

    def close(self) -> None:
        """Hide the drawer (overlay mode only; docked stays open)."""
        if not self.has_class("-docked"):
            self.remove_class("-open")

    def toggle(self) -> None:
        """Toggle overlay visibility. No-op when docked."""
        if self.has_class("-docked"):
            return
        if self.has_class("-open"):
            self.close()
        else:
            self.open()

    @property
    def is_docked(self) -> bool:
        return self.has_class("-docked")

    def action_toggle_dock(self) -> None:
        if self.has_class("-docked"):
            self.remove_class("-docked")
        else:
            self.remove_class("-open")
            self.add_class("-docked")
        self._update_pin_label()

    def _update_pin_label(self) -> None:
        try:
            btn = self.query_one("#drawer-pin-btn", Button)
            btn.label = "📌" if self.has_class("-docked") else "📍"
        except Exception:
            pass

    def action_close(self) -> None:
        self.close()

    # ── Selection ─────────────────────────────────────────────────────────────

    def action_open_selected(self) -> None:
        lst = self.query_one("#project-list", ListView)
        if lst.index is not None and lst.index < len(self._projects):
            path = self._projects[lst.index]
            self._select_project(path)

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
            self.action_toggle_dock()

    def _focus_list(self) -> None:
        try:
            self.query_one("#project-list", ListView).focus()
        except Exception:
            pass

    def on_key(self, event: Key) -> None:
        # Let escape bubble to action_close binding, but also handle here
        # so focus inside input or list still closes
        if event.key == "escape" and not self.has_class("-docked"):
            self.close()
            event.stop()
