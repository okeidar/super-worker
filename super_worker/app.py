import asyncio
import logging
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import ContentSwitcher, Footer, Header, Static

from super_worker.config import ResolvedConfig, load_config
from super_worker.constants import SIDEBAR_REFRESH_S
from super_worker.services.hooks import install_hooks
from super_worker.services.state import (
    load_and_reconcile,
    load_projects_registry,
    remove_from_projects_registry,
)
from super_worker.widgets.project_drawer import (
    DockToggled,
    ProjectDrawer,
    ProjectRemoved,
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
        Binding("ctrl+shift+left", "prev_project", "Prev Project", key_display="ctrl+⇧◀"),
        Binding("ctrl+shift+right", "next_project", "Next Project", key_display="ctrl+⇧▶"),
        Binding("ctrl+e", "edit_settings", "Settings"),
        Binding("ctrl+q", "quit", "Quit"),
        Binding("f12", "debug_screenshot", "Screenshot", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        install_hooks()
        self._active_project_view: ProjectView | None = None
        self._open_configs: list[ResolvedConfig] = []
        self._initial_project: tuple[ResolvedConfig, object] | None = None
        self._attention_paths: set[str] = set()

        try:
            config = load_config()
            state = load_and_reconcile(config)
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
            initial_id = f"pv-{self._initial_project[0].state_hash}" if self._initial_project else None
            with ContentSwitcher(id="project-switcher", initial=initial_id):
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
        self.query_one(ProjectTabBar).show()
        self.set_interval(SIDEBAR_REFRESH_S, self._periodic_refresh)

    # ── Periodic refresh ──────────────────────────────────────────────────────

    def _periodic_refresh(self) -> None:
        if self._active_project_view:
            self.run_worker(
                self._active_project_view.periodic_refresh,
                exclusive=True,
                name="periodic-refresh",
            )
        # Check states for non-active projects so attention indicators update.
        # exclusive=True per project: if a previous check is still running when
        # the next 5s tick fires, cancel it rather than letting workers pile up.
        for cfg in self._open_configs:
            pv_id = f"pv-{cfg.state_hash}"
            try:
                pv = self.query_one(f"#{pv_id}", ProjectView)
                if pv is not self._active_project_view:
                    self.run_worker(
                        pv.check_attention,
                        exclusive=True,
                        group=f"ca-{pv_id}",
                    )
            except Exception:
                pass

    # ── Project drawer / tab bar ──────────────────────────────────────────────

    def _refresh_drawer(self) -> None:
        projects = load_projects_registry()
        current = str(self._active_project_view.config.repo_root) if self._active_project_view else None
        open_paths = {str(cfg.repo_root) for cfg in self._open_configs}
        try:
            self.query_one(ProjectDrawer).refresh_projects(
                projects, current=current, open_paths=open_paths,
                attention_paths=self._attention_paths,
            )
            self.query_one(ProjectTabBar).refresh_projects(
                all_projects=projects, open_paths=open_paths, current=current,
                attention_paths=self._attention_paths,
            )
        except Exception:
            pass

    def action_toggle_project_drawer(self) -> None:
        self.query_one(ProjectDrawer).toggle()

    def on_dock_toggled(self, event: DockToggled) -> None:
        """Switch between overlay drawer and docked tab bar."""
        drawer = self.query_one(ProjectDrawer)
        tab_bar = self.query_one(ProjectTabBar)
        if event.docked:
            drawer.close()
            tab_bar.show()
            self._refresh_drawer()
        else:
            tab_bar.hide()
            drawer.open()

    def on_project_view_attention_changed(self, event: ProjectView.AttentionChanged) -> None:
        """A project's attention state changed — update drawer/tab bar."""
        if event.needs_attention:
            self._attention_paths.add(event.path)
        else:
            self._attention_paths.discard(event.path)
        self._refresh_drawer()

    def on_project_selected(self, event: ProjectSelected) -> None:
        async def _open():
            await self._open_or_switch_project(event.path)

        self.run_worker(_open, exclusive=False)

    def on_project_removed(self, event: ProjectRemoved) -> None:
        remove_from_projects_registry(event.path)
        # If it was open, close it
        self._open_configs = [c for c in self._open_configs if str(c.repo_root) != event.path]
        # If it was active, switch to another open project or show placeholder
        if self._active_project_view and str(self._active_project_view.config.repo_root) == event.path:
            if self._open_configs:
                cfg = self._open_configs[-1]

                async def _reactivate():
                    await self._activate_project(cfg)

                self.run_worker(_reactivate, exclusive=False)
            else:
                self._active_project_view = None
                self.sub_title = ""
        self._refresh_drawer()
        self.notify(f"Removed: {Path(event.path).name}")

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

        new_state = await asyncio.to_thread(load_and_reconcile, new_config)

        pv_id = f"pv-{new_config.state_hash}"
        pv = ProjectView(new_config, new_state, id=pv_id)

        switcher = self.query_one("#project-switcher", ContentSwitcher)

        # Remove the "no project" placeholder if present
        try:
            no_project = self.query_one("#no-project", Static)
            await no_project.remove()
        except Exception:
            pass

        # Pause the outgoing project before mounting the new one
        if self._active_project_view:
            self._active_project_view.pause_watching()

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
            # Pause the outgoing project's capture timer before switching.
            # The kqueue state-file watchers are not paused — they keep running
            # so background projects still update their attention indicators.
            if self._active_project_view:
                self._active_project_view.pause_watching()

            switcher = self.query_one("#project-switcher", ContentSwitcher)
            switcher.current = pv_id
            self._active_project_view = self.query_one(f"#{pv_id}", ProjectView)
            self._active_project_view.resume_watching()
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

    def action_debug_screenshot(self) -> None:
        """Save an SVG screenshot for debugging (F12)."""
        import time
        out = Path("/private/tmp/sw-pilot-screenshots")
        out.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        path = out / f"screenshot_{ts}.svg"
        svg = self.export_screenshot(title=f"sw-{ts}")
        path.write_text(svg)
        self.notify(f"Screenshot: {path.name}")

    def action_prev_project(self) -> None:
        self._cycle_project(-1)

    def action_next_project(self) -> None:
        self._cycle_project(1)

    def _cycle_project(self, direction: int) -> None:
        projects = load_projects_registry()
        if not projects:
            return
        current = str(self._active_project_view.config.repo_root) if self._active_project_view else None
        if current and current in projects:
            idx = (projects.index(current) + direction) % len(projects)
        else:
            idx = 0
        path = projects[idx]

        async def _switch():
            await self._open_or_switch_project(path)

        self.run_worker(_switch, exclusive=False)
