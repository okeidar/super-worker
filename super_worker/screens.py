"""Modal screen dialogs for Super Worker TUI."""

import hashlib
import re

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label

from super_worker.config import ResolvedConfig, SWConfig, WorktreeConfig, EnvConfig, GitConfig, UIConfig, load_toml


class ModalCheckbox(Checkbox):
    """Checkbox that only toggles on space, not enter.

    In modals, enter should submit the form, not toggle a checkbox.
    Space toggles the value. Enter is passed through to the screen.
    """

    _last_key: str = ""

    def on_key(self, event) -> None:
        self._last_key = event.key

    def action_toggle_button(self) -> None:
        if self._last_key == "enter":
            # Don't toggle — find and run the screen's enter binding action
            for binding in self.screen.BINDINGS:
                if "enter" in binding.key:
                    self.call_later(self.screen.run_action, binding.action)
                    return
            return
        super().action_toggle_button()


_NAV_BINDINGS = [
    Binding("down", "focus_next_field", "Next field", show=False),
    Binding("up", "focus_prev_field", "Previous field", show=False),
]


class _ModalNavMixin:
    """Mixin adding up/down arrow navigation between focusable widgets in modals."""

    def action_focus_next_field(self) -> None:
        self.focus_next()

    def action_focus_prev_field(self) -> None:
        self.focus_previous()


class NewWorktreeScreen(_ModalNavMixin, ModalScreen[tuple[str, str | None, str | None, bool, bool] | None]):
    """Modal dialog for creating a new worktree."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "submit", "Create", show=False),
        *_NAV_BINDINGS,
    ]

    DEFAULT_CSS = """
    NewWorktreeScreen {
        align: center middle;
    }
    #new-wt-dialog {
        width: 60;
        height: 22;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    """

    def __init__(self, config: ResolvedConfig) -> None:
        super().__init__()
        self._config = config

    def compose(self) -> ComposeResult:
        with Vertical(id="new-wt-dialog"):
            yield Label("New Worktree")
            yield Label("Name:")
            yield Input(placeholder=self._config.name_placeholder, id="wt-name")
            yield Label(f"Branch name (optional, defaults to {self._config.branch_placeholder}):")
            yield Input(placeholder=self._config.branch_placeholder, id="wt-branch")
            yield Label("Initial prompt (optional, e.g. /plan):")
            yield Input(placeholder="/plan", id="wt-prompt")
            yield ModalCheckbox("No new branch (detached HEAD)", id="wt-detach")
            yield ModalCheckbox("Skip permissions", id="wt-skip-perms")
            yield Label("Press Enter to create, Escape to cancel")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_submit()

    def action_submit(self) -> None:
        name = self.query_one("#wt-name", Input).value.strip()
        if not name:
            return
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
            self.notify("Name must contain only letters, digits, hyphens, and underscores", severity="error")
            return
        branch = self.query_one("#wt-branch", Input).value.strip() or None
        prompt = self.query_one("#wt-prompt", Input).value.strip() or None
        detach = self.query_one("#wt-detach", Checkbox).value
        skip_perms = self.query_one("#wt-skip-perms", Checkbox).value
        self.dismiss((name, branch, prompt, detach, skip_perms))

    def action_cancel(self) -> None:
        self.dismiss(None)


class NewSessionScreen(_ModalNavMixin, ModalScreen[tuple[str | None, str | None, bool] | None]):
    """Modal dialog for adding a session to the current worktree."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "submit", "Create", show=False),
        *_NAV_BINDINGS,
    ]

    DEFAULT_CSS = """
    NewSessionScreen {
        align: center middle;
    }
    #new-sess-dialog {
        width: 60;
        height: 16;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="new-sess-dialog"):
            yield Label("New Session")
            yield Label("Prompt (optional, e.g. /plan):")
            yield Input(placeholder="/execute", id="sess-prompt")
            yield Label("Label (optional):")
            yield Input(placeholder="", id="sess-label")
            yield ModalCheckbox("Skip permissions", id="sess-skip-perms")
            yield Label("Press Enter to create, Escape to cancel")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_submit()

    def action_submit(self) -> None:
        prompt = self.query_one("#sess-prompt", Input).value.strip() or None
        label = self.query_one("#sess-label", Input).value.strip() or None
        skip_perms = self.query_one("#sess-skip-perms", Checkbox).value
        self.dismiss((prompt, label, skip_perms))

    def action_cancel(self) -> None:
        self.dismiss(None)


class RenameSessionScreen(_ModalNavMixin, ModalScreen[str | None]):
    """Modal dialog for renaming a session."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "submit", "Rename", show=False),
        *_NAV_BINDINGS,
    ]

    DEFAULT_CSS = """
    RenameSessionScreen {
        align: center middle;
    }
    #rename-sess-dialog {
        width: 60;
        height: 10;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    """

    def __init__(self, current_label: str) -> None:
        super().__init__()
        self._current_label = current_label

    def compose(self) -> ComposeResult:
        with Vertical(id="rename-sess-dialog"):
            yield Label("Rename Session")
            yield Input(value=self._current_label, id="rename-input")
            yield Label("Press Enter to rename, Escape to cancel")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_submit()

    def action_submit(self) -> None:
        new_label = self.query_one("#rename-input", Input).value.strip()
        self.dismiss(new_label if new_label else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmDeleteScreen(_ModalNavMixin, ModalScreen[bool]):
    """Confirmation dialog for deleting a worktree."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "confirm", "Confirm", show=False),
        *_NAV_BINDINGS,
    ]

    DEFAULT_CSS = """
    ConfirmDeleteScreen {
        align: center middle;
    }
    #confirm-dialog {
        width: 50;
        height: 10;
        border: thick $error;
        background: $surface;
        padding: 1 2;
    }
    #confirm-buttons {
        height: 3;
        align: center middle;
    }
    #confirm-buttons Button {
        margin: 0 2;
    }
    """

    def __init__(self, worktree_name: str) -> None:
        super().__init__()
        self._worktree_name = worktree_name

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Label(f"Delete worktree '{self._worktree_name}'?")
            yield Label("This will kill all sessions and remove the\nworktree directory. Branch is kept.")
            with Horizontal(id="confirm-buttons"):
                yield Button("Delete", variant="error", id="btn-confirm")
                yield Button("Cancel", variant="default", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "btn-confirm")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class CommitMessageScreen(_ModalNavMixin, ModalScreen[str | None]):
    """Modal dialog for entering a commit message."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "submit", "Commit", show=False),
        *_NAV_BINDINGS,
    ]

    DEFAULT_CSS = """
    CommitMessageScreen {
        align: center middle;
    }
    #commit-dialog {
        width: 70;
        height: 10;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    """

    def __init__(self, commit_placeholder: str = "Brief description of changes") -> None:
        super().__init__()
        self._commit_placeholder = commit_placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="commit-dialog"):
            yield Label("Commit Message")
            yield Input(placeholder=self._commit_placeholder, id="commit-msg")
            yield Label("Press Enter to commit all changes, Escape to cancel")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_submit()

    def action_submit(self) -> None:
        msg = self.query_one("#commit-msg", Input).value.strip()
        if msg:
            self.dismiss(msg)

    def action_cancel(self) -> None:
        self.dismiss(None)


class BranchExistsScreen(_ModalNavMixin, ModalScreen[str]):
    """Ask user what to do when branch already exists."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "use_existing", "Use Existing", show=False),
        *_NAV_BINDINGS,
    ]

    DEFAULT_CSS = """
    BranchExistsScreen {
        align: center middle;
    }
    #branch-dialog {
        width: 55;
        height: 10;
        border: thick $warning;
        background: $surface;
        padding: 1 2;
    }
    #branch-buttons {
        height: 3;
        align: center middle;
    }
    #branch-buttons Button {
        margin: 0 1;
    }
    """

    def __init__(self, branch: str) -> None:
        super().__init__()
        self._branch = branch

    def compose(self) -> ComposeResult:
        with Vertical(id="branch-dialog"):
            yield Label(f"Branch '{self._branch}' already exists.")
            yield Label("Use existing branch or create new with different name?")
            with Horizontal(id="branch-buttons"):
                yield Button("Use Existing", variant="primary", id="btn-use")
                yield Button("Cancel", variant="default", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-use":
            self.dismiss("use")
        else:
            self.dismiss("cancel")

    def action_use_existing(self) -> None:
        self.dismiss("use")

    def action_cancel(self) -> None:
        self.dismiss("cancel")


class ProjectSelectorScreen(_ModalNavMixin, ModalScreen[str | None]):
    """Modal to select a project from known repos or browse for a new one."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "submit", "Select", show=False),
        *_NAV_BINDINGS,
    ]

    DEFAULT_CSS = """
    ProjectSelectorScreen {
        align: center middle;
    }
    #project-dialog {
        width: 80;
        height: auto;
        max-height: 80%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #project-dialog Button {
        width: 100%;
        margin: 0 0 0 0;
    }
    #browse-input {
        margin-top: 1;
    }
    """

    def __init__(self, projects: list[str], current: str) -> None:
        super().__init__()
        self._projects = projects
        self._current = current

    @staticmethod
    def _proj_id(path: str) -> str:
        return f"proj-{hashlib.sha256(path.encode()).hexdigest()[:8]}"

    def compose(self) -> ComposeResult:
        with Vertical(id="project-dialog"):
            yield Label("Open Project")
            for p in self._projects:
                marker = " (current)" if p == self._current else ""
                yield Button(f"{p}{marker}", id=self._proj_id(p), variant="primary" if p == self._current else "default")
            yield Label("Or enter a path to a git repo:")
            yield Input(placeholder="/path/to/repo", id="browse-input")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id or ""
        for p in self._projects:
            if btn_id == self._proj_id(p):
                self.dismiss(p)
                return

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_submit()

    def action_submit(self) -> None:
        path = self.query_one("#browse-input", Input).value.strip()
        if path:
            self.dismiss(path)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfigScreen(_ModalNavMixin, ModalScreen[SWConfig | None]):
    """Modal to edit project .sw.toml settings."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "save", "Save", show=False),
        *_NAV_BINDINGS,
    ]

    DEFAULT_CSS = """
    ConfigScreen {
        align: center middle;
    }
    #config-dialog {
        width: 80;
        height: auto;
        max-height: 90%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
        overflow-y: auto;
    }
    .config-section {
        height: 1;
        text-style: bold;
        color: $accent;
        margin-top: 1;
    }
    .config-label {
        height: 1;
        color: $text-muted;
    }
    #config-buttons {
        height: 3;
        align: center middle;
        margin-top: 1;
    }
    #config-buttons Button {
        margin: 0 2;
    }
    """

    def __init__(self, config: ResolvedConfig) -> None:
        super().__init__()
        self._config = config
        self._project_cfg = load_toml(config.repo_root / ".sw.toml")

    def compose(self) -> ComposeResult:
        cfg = self._project_cfg
        with Vertical(id="config-dialog"):
            yield Label("Project Settings")

            yield Label("[worktree]", classes="config-section")
            yield Label("Branch prefix:", classes="config-label")
            yield Input(value=cfg.worktree.branch_prefix, placeholder="sw-", id="cfg-branch-prefix")
            yield Label("Worktree dir prefix:", classes="config-label")
            yield Input(value=cfg.worktree.prefix, placeholder=self._config.repo_root.name, id="cfg-prefix")
            yield Label("Base directory:", classes="config-label")
            yield Input(value=cfg.worktree.base_dir, placeholder=str(self._config.repo_root.parent), id="cfg-base-dir")

            yield Label("[env]", classes="config-section")
            yield Label("Symlinks (comma-separated):", classes="config-label")
            yield Input(value=", ".join(cfg.env.symlinks), placeholder=".venv, .claude", id="cfg-symlinks")
            yield Label("Copies (comma-separated):", classes="config-label")
            yield Input(value=", ".join(cfg.env.copies), placeholder=".env", id="cfg-copies")
            yield Label("Post-create hook:", classes="config-label")
            yield Input(value=cfg.env.post_create_hook, placeholder="path/to/script.sh", id="cfg-hook")

            yield Label("[git]", classes="config-section")
            yield Label("Main branch:", classes="config-label")
            yield Input(value=cfg.git.main_branch, placeholder=self._config.main_branch, id="cfg-main-branch")
            yield Label("Remote:", classes="config-label")
            yield Input(value=cfg.git.remote, placeholder=self._config.remote, id="cfg-remote")

            yield Label("[ui]", classes="config-section")
            yield Label("Commit placeholder:", classes="config-label")
            yield Input(value=cfg.ui.commit_placeholder, placeholder="Brief description of changes", id="cfg-commit")
            yield Label("Name placeholder:", classes="config-label")
            yield Input(value=cfg.ui.name_placeholder, placeholder="feature-name", id="cfg-name")
            yield Label("Branch placeholder:", classes="config-label")
            yield Input(value=cfg.ui.branch_placeholder, placeholder=f"{self._config.branch_prefix}<name>", id="cfg-branch-ph")

            with Horizontal(id="config-buttons"):
                yield Button("Save", variant="primary", id="btn-save")
                yield Button("Cancel", variant="default", id="btn-cfg-cancel")

    def _collect(self) -> SWConfig:
        def val(id: str) -> str:
            return self.query_one(f"#{id}", Input).value.strip()

        def csv(id: str) -> list[str]:
            raw = val(id)
            return [s.strip() for s in raw.split(",") if s.strip()] if raw else []

        return SWConfig(
            worktree=WorktreeConfig(prefix=val("cfg-prefix"), branch_prefix=val("cfg-branch-prefix"), base_dir=val("cfg-base-dir")),
            env=EnvConfig(symlinks=csv("cfg-symlinks"), copies=csv("cfg-copies"), post_create_hook=val("cfg-hook")),
            git=GitConfig(main_branch=val("cfg-main-branch"), remote=val("cfg-remote")),
            ui=UIConfig(commit_placeholder=val("cfg-commit"), name_placeholder=val("cfg-name"), branch_placeholder=val("cfg-branch-ph")),
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-save":
            self.dismiss(self._collect())
        else:
            self.dismiss(None)

    def action_save(self) -> None:
        self.dismiss(self._collect())

    def action_cancel(self) -> None:
        self.dismiss(None)
