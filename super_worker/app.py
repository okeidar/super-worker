import asyncio
import logging
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import ContentSwitcher, Footer, Header, Static

from super_worker.config import ResolvedConfig, load_config
from super_worker.constants import SIDEBAR_REFRESH_S
from super_worker.services.state import (
    load_projects_registry,
    load_state,
    reconcile_state,
    recover_dead_sessions,
    save_state,
    update_projects_registry,
)
from super_worker.widgets.project_drawer import (
    DockToggled,
    ProjectDrawer,
    ProjectSelected,
    ProjectTabBar,
)
from super_worker.widgets.project_view import ProjectView

logger = logging.getLogger(__name__)


class SuperWorkerApp(App):
    """Super Worker — Claude Code Instance Manager TUI."""

    TITLE = "Super Worker"

    DEFAULT_CSS = """
    #main-area {
        height: 1fr;
    }
    #project-switcher {
        width: 1fr;
        height: 1fr;
    }
    #no-project {
        width: 100%;
        height: 100%;
        content-align: center middle;
        text-style: italic;
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("ctrl+n", "new_worktree", "New Worktree"),
        Binding("ctrl+s", "new_session", "New Session"),
        Binding("ctrl+a", "full_attach", "Full Attach"),
        Binding("ctrl+t", "open_terminal", "Open Terminal"),
        Binding("ctrl+r", "rename_session", "Rename Session"),
        Binding("ctrl+d", "delete_worktree", "Delete Worktree"),
        Binding("ctrl+o", "toggle_project_drawer", "Projects"),
        Binding("ctrl+e", "edit_settings", "Settings"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._active_project_view: ProjectView | None = None
        self._open_configs: list[ResolvedConfig] = []
        self._initial_project: tuple[ResolvedConfig, object] | None = None

        try:
            config = load_config()
            state = load_state(config)
            update_projects_registry(config)
            changed = reconcile_state(state, config)
            changed = recover_dead_sessions(state) or changed
            if changed:
                save_state(state, config)
            self._initial_project = (config, state)
            self._open_configs.append(config)
        except RuntimeError:
            pass  # Started outside a git repo; drawer will prompt

    def compose(self) -> ComposeResult:
        yield Header()
        # Docked mode: horizontal tab strip sits here, above worktree tabs.
        # Hidden by default; shown when user presses the drawer's pin button.
        yield ProjectTabBar(id="project-tab-bar")
        with Horizontal(id="main-area"):
            # Overlay mode: left-side drawer, hidden by default (Ctrl+O to toggle).
            yield ProjectDrawer(id="project-drawer")
            with ContentSwitcher(id="project-switcher"):
                if self._initial_project:
                    config, state = self._initial_project
                    yield ProjectView(config, state, id=f"pv-{config.state_hash}")
                else:
                    yield Static(
                        "No project open.\nPress Ctrl+O to open a project.",
                        id="no-project",
                    )
        yield Footer()

    def on_mount(self) -> None:
        if self._initial_project:
            config, _ = self._initial_project
            try:
                pv = self.query_one(f"#pv-{config.state_hash}", ProjectView)
                self._active_project_view = pv
                self.sub_title = str(config.repo_root)
            except Exception:
                pass
        else:
            # Auto-open drawer so user can pick a project
            self.call_after_refresh(lambda: self.query_one(ProjectDrawer).open())

        self._refresh_drawer()
        self.set_interval(SIDEBAR_REFRESH_S, self._periodic_refresh)

    # ── Periodic refresh ──────────────────────────────────────────────────────

    def _periodic_refresh(self) -> None:
        if self._active_project_view:
            self.run_worker(
                self._active_project_view.periodic_refresh,
                exclusive=True,
                name="periodic-refresh",
            )

    # ── Project drawer / tab bar ──────────────────────────────────────────────

    def _refresh_drawer(self) -> None:
        projects = load_projects_registry()
        current = str(self._active_project_view.config.repo_root) if self._active_project_view else None
        open_paths = {str(cfg.repo_root) for cfg in self._open_configs}
        try:
            self.query_one(ProjectDrawer).refresh_projects(projects, current=current, open_paths=open_paths)
            self.query_one(ProjectTabBar).refresh_projects(open_paths=open_paths, current=current)
        except Exception:
            pass

    def action_toggle_project_drawer(self) -> None:
        tab_bar = self.query_one(ProjectTabBar)
        if tab_bar.has_class("-visible"):
            # Already docked — Ctrl+O re-opens the floating drawer on top
            self.query_one(ProjectDrawer).open()
        else:
            self.query_one(ProjectDrawer).toggle()

    def on_dock_toggled(self, event: DockToggled) -> None:
        """Switch between overlay drawer and docked tab bar."""
        drawer = self.query_one(ProjectDrawer)
        tab_bar = self.query_one(ProjectTabBar)
        if event.docked:
            drawer.close()
            tab_bar.show()
        else:
            tab_bar.hide()
            drawer.open()

    def on_project_selected(self, event: ProjectSelected) -> None:
        async def _open():
            await self._open_or_switch_project(event.path)

        self.run_worker(_open, exclusive=False)

    async def _open_or_switch_project(self, path: str) -> None:
        """Switch to an already-open project or load a new one."""
        # Already open?
        for cfg in self._open_configs:
            if str(cfg.repo_root) == path:
                await self._activate_project(cfg)
                return

        # Load fresh
        try:
            new_config = await asyncio.to_thread(load_config, Path(path))
        except RuntimeError as e:
            self.notify(str(e), severity="error")
            return

        new_state = await asyncio.to_thread(load_state, new_config)
        await asyncio.to_thread(update_projects_registry, new_config)
        changed = await asyncio.to_thread(reconcile_state, new_state, new_config)
        changed = await asyncio.to_thread(recover_dead_sessions, new_state) or changed
        if changed:
            await asyncio.to_thread(save_state, new_state, new_config)

        pv_id = f"pv-{new_config.state_hash}"
        pv = ProjectView(new_config, new_state, id=pv_id)

        switcher = self.query_one("#project-switcher", ContentSwitcher)

        # Remove the "no project" placeholder if present
        try:
            no_project = self.query_one("#no-project", Static)
            await no_project.remove()
        except Exception:
            pass

        await switcher.mount(pv)
        switcher.current = pv_id
        self._active_project_view = pv
        self._open_configs.append(new_config)
        self.sub_title = str(new_config.repo_root)
        self._refresh_drawer()
        self.notify(f"Opened: {new_config.repo_root.name}")

    async def _activate_project(self, config: ResolvedConfig) -> None:
        """Switch focus to an already-mounted ProjectView."""
        pv_id = f"pv-{config.state_hash}"
        try:
            switcher = self.query_one("#project-switcher", ContentSwitcher)
            switcher.current = pv_id
            self._active_project_view = self.query_one(f"#{pv_id}", ProjectView)
            self.sub_title = str(config.repo_root)
            self._refresh_drawer()
        except Exception:
            logger.debug("Failed to activate project", exc_info=True)

    # ── Action delegation to active ProjectView ───────────────────────────────

    def action_new_worktree(self) -> None:
        if pv := self._active_project_view:
            pv.do_new_worktree()
        else:
            self.notify("Open a project first (Ctrl+O)", severity="warning")

    def action_new_session(self) -> None:
        if pv := self._active_project_view:
            pv.do_new_session()
        else:
            self.notify("Open a project first (Ctrl+O)", severity="warning")

    def action_rename_session(self) -> None:
        if pv := self._active_project_view:
            pv.do_rename_session()

    def action_full_attach(self) -> None:
        if pv := self._active_project_view:
            pv.do_full_attach()

    def action_open_terminal(self) -> None:
        if pv := self._active_project_view:
            pv.do_open_terminal()

    def action_edit_settings(self) -> None:
        if pv := self._active_project_view:
            pv.do_edit_settings()
        else:
            self.notify("Open a project first (Ctrl+O)", severity="warning")

    def action_delete_worktree(self) -> None:
        if pv := self._active_project_view:
            pv.do_delete_worktree()
