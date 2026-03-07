"""Per-project widget: worktree tabs, session management, git actions."""

import asyncio
import logging
import platform
import shlex
import shutil
import subprocess
import webbrowser

import git as gitpython
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widget import Widget
from textual.widgets import Static, TabPane, TabbedContent

from super_worker.config import ResolvedConfig, SWConfig, load_config, save_project_config
from super_worker.constants import DEFAULT_WORKTREE_NAME
from super_worker.models import AppState, Worktree
from super_worker.screens import (
    BranchExistsScreen,
    CommitMessageScreen,
    ConfigScreen,
    ConfirmDeleteScreen,
    NewSessionScreen,
    NewWorktreeScreen,
    RenameSessionScreen,
)
from super_worker.services.state import (
    remove_session_from_state,
    remove_worktree_from_state,
    save_state,
)
from super_worker.services.tmux import (
    SessionState,
    batch_detect_session_states,
    create_session,
    enable_mouse,
    kill_all_sessions,
    kill_session,
)
from super_worker.services.worktree import (
    BranchExistsError,
    create_worktree,
    get_branch_status,
    get_current_branch,
    get_worktree_dirty,
    invalidate_git_cache,
    remove_worktree,
)
from super_worker.widgets.sidebar import GitAction, SessionDeleted, SessionSelected, SessionSidebar
from super_worker.widgets.terminal_pane import TerminalPane

logger = logging.getLogger(__name__)


class WorktreeTabContent(Horizontal):
    """Sidebar + terminal for a single worktree tab."""

    DEFAULT_CSS = """
    WorktreeTabContent {
        height: 1fr;
        width: 1fr;
    }
    """

    def __init__(self, worktree: Worktree, remote: str = "origin", main_branch: str = "main") -> None:
        super().__init__(id=f"wtc-{worktree.name}")
        self.worktree = worktree
        self._remote = remote
        self._main_branch = main_branch

    def compose(self) -> ComposeResult:
        yield SessionSidebar(remote=self._remote, main_branch=self._main_branch)
        yield TerminalPane()

    def on_mount(self) -> None:
        async def _init_sidebar() -> None:
            if not self.worktree.sessions:
                session = await asyncio.to_thread(create_session, self.worktree)
                self.worktree.sessions.append(session)

            session_names = [s.tmux_session_name for s in self.worktree.sessions]
            states = await asyncio.to_thread(batch_detect_session_states, session_names) if session_names else {}
            status = await asyncio.to_thread(get_branch_status, self.worktree.path, self._remote, self._main_branch)
            dirty = await asyncio.to_thread(get_worktree_dirty, self.worktree.path)
            sidebar = self.query_one(SessionSidebar)
            sidebar.show_worktree(self.worktree, states=states, git_status=status, git_dirty=dirty)

            if self.worktree.sessions:
                first = self.worktree.sessions[0]
                terminal = self.query_one(TerminalPane)
                terminal.active_session = first.tmux_session_name

        self.app.run_worker(_init_sidebar, exclusive=False)


class ProjectView(Widget):
    """Self-contained per-project widget: worktree tabs + session/git management."""

    DEFAULT_CSS = """
    ProjectView {
        width: 1fr;
        height: 1fr;
    }
    TabbedContent {
        height: 1fr;
    }
    #empty-state {
        width: 100%;
        height: 100%;
        content-align: center middle;
        text-style: italic;
        color: $text-muted;
    }
    """

    def __init__(self, config: ResolvedConfig, state: AppState, **kwargs) -> None:
        super().__init__(**kwargs)
        self._config = config
        self._state = state
        self._active_worktree: Worktree | None = None
        self._active_session_name: str | None = None
        self._cached_session_states: dict[str, SessionState] = {}
        self._ensure_default_worktree()

    @property
    def config(self) -> ResolvedConfig:
        return self._config

    @property
    def state(self) -> AppState:
        return self._state

    def _ensure_default_worktree(self) -> None:
        existing = self._state.get_worktree(DEFAULT_WORKTREE_NAME)
        if existing:
            existing.branch = get_current_branch(str(self._config.repo_root))
            if not existing.sessions:
                session = create_session(existing)
                existing.sessions.append(session)
                save_state(self._state, self._config)
            return

        branch = get_current_branch(str(self._config.repo_root))
        wt = Worktree(name=DEFAULT_WORKTREE_NAME, path=str(self._config.repo_root), branch=branch)
        session = create_session(wt)
        wt.sessions.append(session)
        self._state.worktrees.insert(0, wt)
        save_state(self._state, self._config)

    def compose(self) -> ComposeResult:
        if self._state.worktrees:
            with TabbedContent(id="tabs"):
                for wt in self._state.worktrees:
                    with TabPane(self._tab_label(wt), id=f"wt-{wt.name}"):
                        yield WorktreeTabContent(wt, self._config.remote, self._config.main_branch)
        else:
            yield Static("No worktrees. Press Ctrl+N to create one.", id="empty-state")

    def on_mount(self) -> None:
        if self._state.worktrees:
            self._set_active_worktree(self._state.worktrees[0])

    def _tab_label(self, wt: Worktree, git_data: tuple[dict, bool] | None = None) -> str:
        if git_data is None:
            return wt.name
        status, dirty = git_data
        dirty_marker = " *" if dirty else ""
        attention = ""
        for s in wt.sessions:
            state = self._cached_session_states.get(s.tmux_session_name, SessionState.RUNNING)
            if state in (SessionState.WAITING_INPUT, SessionState.WAITING_APPROVAL):
                attention = " 🔔"
                break
        return f"{wt.name} (↑{status['ahead']} ↓{status['behind']}){dirty_marker}{attention}"

    def _set_active_worktree(self, wt: Worktree) -> None:
        self._active_worktree = wt
        if wt.sessions:
            self._active_session_name = wt.sessions[0].tmux_session_name

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        tab_id = event.pane.id
        if tab_id and tab_id.startswith("wt-"):
            name = tab_id[3:]
            wt = self._state.get_worktree(name)
            if wt:
                self._set_active_worktree(wt)

    def on_session_selected(self, event: SessionSelected) -> None:
        self._active_worktree = event.worktree
        self._active_session_name = event.session.tmux_session_name
        try:
            wtc = self.query_one(f"#wtc-{event.worktree.name}", WorktreeTabContent)
            terminal = wtc.query_one(TerminalPane)
            terminal.active_session = event.session.tmux_session_name
            terminal.focus()
        except Exception:
            logger.debug("Failed to activate session in terminal pane", exc_info=True)

    async def on_session_deleted(self, event: SessionDeleted) -> None:
        wt = event.worktree
        session = event.session
        tmux_name = session.tmux_session_name

        try:
            wtc = self.query_one(f"#wtc-{wt.name}", WorktreeTabContent)
            terminal = wtc.query_one(TerminalPane)
            if terminal.active_session == tmux_name:
                terminal.active_session = None
        except Exception:
            pass
        if self._active_session_name == tmux_name:
            self._active_session_name = None

        self._state = remove_session_from_state(self._state, wt.name, session.id)
        try:
            wtc = self.query_one(f"#wtc-{wt.name}", WorktreeTabContent)
            sidebar = wtc.query_one(SessionSidebar)
            sidebar._prev_session_snapshot = "__deleted__"
            sidebar.show_worktree(wt, states={}, git_status={"ahead": 0, "behind": 0}, git_dirty=False)
        except Exception:
            pass

        if wt.sessions:
            next_session = wt.sessions[0]
            self._active_session_name = next_session.tmux_session_name
            try:
                wtc = self.query_one(f"#wtc-{wt.name}", WorktreeTabContent)
                wtc.query_one(TerminalPane).active_session = next_session.tmux_session_name
            except Exception:
                pass

        self.app.notify(f"Deleted session: {session.label}")
        await asyncio.to_thread(kill_session, tmux_name)
        await asyncio.to_thread(save_state, self._state, self._config)

    def on_git_action(self, event: GitAction) -> None:
        wt = event.worktree
        if event.action == "commit":
            self._git_commit(wt)
        elif event.action == "push":
            self._git_push(wt)
        elif event.action == "pull":
            self._git_pull(wt)
        elif event.action == "pr":
            self._git_create_pr(wt)

    # ── Public delegation API ─────────────────────────────────────────────────

    def do_new_worktree(self) -> None:
        def handle_result(result: tuple[str, str | None, str | None, bool, bool] | None) -> None:
            if result is None:
                return
            name, branch, prompt, detach, skip_perms = result
            if self._state.get_worktree(name):
                self.app.notify(f"Worktree '{name}' already exists", severity="error")
                return
            self._create_worktree(name, prompt, branch=branch, use_existing_branch=False, detach=detach, skip_permissions=skip_perms)

        self.app.push_screen(NewWorktreeScreen(self._config), callback=handle_result)

    def _create_worktree(
        self,
        name: str,
        prompt: str | None,
        branch: str | None = None,
        use_existing_branch: bool = False,
        detach: bool = False,
        skip_permissions: bool = False,
    ) -> None:
        async def _create() -> None:
            try:
                wt = await asyncio.to_thread(
                    create_worktree, self._config, name,
                    branch=branch, use_existing_branch=use_existing_branch, detach=detach,
                    worktree_index=len(self._state.worktrees),
                )
            except BranchExistsError as e:
                def handle_branch(choice: str) -> None:
                    if choice == "use":
                        self._create_worktree(name, prompt, branch=branch, use_existing_branch=True, detach=detach, skip_permissions=skip_permissions)
                self.app.push_screen(BranchExistsScreen(e.branch), callback=handle_branch)
                return
            except Exception as e:
                self.app.notify(str(e), severity="error")
                return

            self._state.worktrees.append(wt)
            if prompt:
                session = await asyncio.to_thread(create_session, wt, prompt=prompt, label=prompt, skip_permissions=skip_permissions)
                wt.sessions.append(session)
            await asyncio.to_thread(save_state, self._state, self._config)
            await self._add_worktree_tab(wt)
            self.app.notify(f"Created worktree: {name}")

        self.run_worker(_create, exclusive=False)

    async def _add_worktree_tab(self, wt: Worktree) -> None:
        try:
            empty = self.query_one("#empty-state", Static)
            await empty.remove()
            tabs = TabbedContent(id="tabs")
            await self.mount(tabs)
        except Exception:
            tabs = self.query_one("#tabs", TabbedContent)

        pane = TabPane(self._tab_label(wt), id=f"wt-{wt.name}")
        pane.compose_add_child(WorktreeTabContent(wt, self._config.remote, self._config.main_branch))
        await tabs.add_pane(pane)
        tabs.active = f"wt-{wt.name}"
        self._set_active_worktree(wt)

    def do_new_session(self) -> None:
        if not self._active_worktree:
            self.app.notify("Select a worktree first", severity="warning")
            return
        wt = self._active_worktree

        def handle_result(result: tuple[str, str | None, str | None, bool] | None) -> None:
            if result is None:
                return
            session_type, prompt, label, skip_perms = result

            async def _create_session() -> None:
                try:
                    session = await asyncio.to_thread(
                        create_session, wt, prompt=prompt, label=label,
                        skip_permissions=skip_perms, session_type=session_type,
                    )
                    wt.sessions.append(session)
                    await asyncio.to_thread(save_state, self._state, self._config)
                except Exception as e:
                    self.app.notify(str(e), severity="error")
                    return

                self._active_session_name = session.tmux_session_name
                await self._refresh_sidebar(wt)
                wtc = self.query_one(f"#wtc-{wt.name}", WorktreeTabContent)
                terminal = wtc.query_one(TerminalPane)
                terminal.active_session = session.tmux_session_name
                terminal.focus()
                self.app.notify(f"Created session: {session.label}")

            self.run_worker(_create_session, exclusive=False)

        self.app.push_screen(NewSessionScreen(), callback=handle_result)

    def do_rename_session(self) -> None:
        if not self._active_worktree or not self._active_session_name:
            self.app.notify("No active session to rename", severity="warning")
            return
        wt = self._active_worktree
        session = next((s for s in wt.sessions if s.tmux_session_name == self._active_session_name), None)
        if not session:
            return

        def handle_rename(new_label: str | None) -> None:
            if not new_label:
                return
            session.label = new_label

            async def _save_and_refresh() -> None:
                await asyncio.to_thread(save_state, self._state, self._config)
                await self._refresh_sidebar(wt)
                self.app.notify(f"Renamed session to: {new_label}")

            self.run_worker(_save_and_refresh, exclusive=False)

        self.app.push_screen(RenameSessionScreen(session.label), callback=handle_rename)

    def do_full_attach(self) -> None:
        if not self._active_worktree or not self._active_session_name:
            self.app.notify("No active session to attach", severity="warning")
            return
        session_name = self._active_session_name
        try:
            wtc = self.query_one(f"#wtc-{self._active_worktree.name}", WorktreeTabContent)
            terminal = wtc.query_one(TerminalPane)
            terminal.active_session = None
        except Exception:
            logger.debug("Failed to pause terminal before attach", exc_info=True)
        enable_mouse(session_name)
        with self.app.suspend():
            q = shlex.quote(session_name)
            subprocess.run([
                "bash", "-c",
                "printf '\\e[?1000l\\e[?1003l\\e[?1015l\\e[?1006l' && "
                f"tmux attach-session -t {q}",
            ])
        try:
            wtc = self.query_one(f"#wtc-{self._active_worktree.name}", WorktreeTabContent)
            terminal = wtc.query_one(TerminalPane)
            terminal.active_session = session_name
        except Exception:
            logger.debug("Failed to resume terminal after attach", exc_info=True)

    def do_open_terminal(self) -> None:
        if not self._active_session_name:
            self.app.notify("No active session to open", severity="warning")
            return
        session_name = self._active_session_name

        async def _open() -> None:
            await asyncio.to_thread(enable_mouse, session_name)
            attach_cmd = f"tmux attach-session -t {shlex.quote(session_name)}"
            system = platform.system()
            if system == "Darwin":
                subprocess.Popen([
                    "osascript", "-e",
                    f'tell application "Terminal" to do script "{attach_cmd}"',
                ])
            else:
                for term in ("x-terminal-emulator", "gnome-terminal", "xterm"):
                    if shutil.which(term):
                        subprocess.Popen([term, "-e", "bash", "-c", attach_cmd])
                        return
                self.app.notify("No terminal emulator found. Use Ctrl+A to attach.", severity="warning")

        self.run_worker(_open, exclusive=False)

    def do_edit_settings(self) -> None:
        def handle_config(result: SWConfig | None) -> None:
            if result is None:
                return
            save_project_config(self._config.repo_root, result)
            self._config = load_config(self._config.repo_root)
            self.app.notify("Settings saved. Some changes take effect on next worktree creation.")

        self.app.push_screen(ConfigScreen(self._config), callback=handle_config)

    def do_delete_worktree(self) -> None:
        if not self._active_worktree:
            self.app.notify("No worktree selected", severity="warning")
            return
        wt = self._active_worktree
        if wt.name == DEFAULT_WORKTREE_NAME:
            self.app.notify("Cannot delete the main worktree", severity="warning")
            return

        wt_name = wt.name

        def handle_confirm(confirmed: bool) -> None:
            if not confirmed:
                return

            async def _delete() -> None:
                target = self._state.get_worktree(wt_name)
                if not target:
                    return
                try:
                    await asyncio.to_thread(kill_all_sessions, target)
                    await asyncio.to_thread(remove_worktree, self._state, wt_name, force=True)
                    self._state = remove_worktree_from_state(self._state, wt_name)
                    await asyncio.to_thread(save_state, self._state, self._config)
                except Exception as e:
                    self.app.notify(str(e), severity="error")
                    return

                self._active_worktree = None
                self._active_session_name = None
                await self._remove_worktree_tab(wt.name)
                self.app.notify(f"Deleted worktree: {wt.name}")

            self.run_worker(_delete, exclusive=False)

        self.app.push_screen(ConfirmDeleteScreen(wt.name), callback=handle_confirm)

    async def _remove_worktree_tab(self, name: str) -> None:
        try:
            tabs = self.query_one("#tabs", TabbedContent)
            await tabs.remove_pane(f"wt-{name}")
            if not self._state.worktrees:
                await tabs.remove()
                await self.mount(Static("No worktrees. Press Ctrl+N to create one.", id="empty-state"))
            else:
                active_tab = tabs.active
                if active_tab and active_tab.startswith("wt-"):
                    wt_name = active_tab[3:]
                    wt = self._state.get_worktree(wt_name)
                    if wt:
                        self._set_active_worktree(wt)
                        return
                self._set_active_worktree(self._state.worktrees[0])
        except Exception:
            logger.debug("Failed to remove worktree tab", exc_info=True, extra={"name": name})

    # ── Periodic refresh ──────────────────────────────────────────────────────

    async def periodic_refresh(self) -> None:
        """Fetch all blocking data in threads, then update UI. Called by app timer."""
        all_session_names = [s.tmux_session_name for wt in self._state.worktrees for s in wt.sessions]
        if all_session_names:
            self._cached_session_states = await asyncio.to_thread(batch_detect_session_states, all_session_names)
        else:
            self._cached_session_states = {}

        git_data: dict[str, tuple[dict, bool]] = {}
        if self._state.worktrees:
            tasks = []
            for wt in self._state.worktrees:
                tasks.append(asyncio.to_thread(get_branch_status, wt.path, self._config.remote, self._config.main_branch))
                tasks.append(asyncio.to_thread(get_worktree_dirty, wt.path))
            results = await asyncio.gather(*tasks)
            for i, wt in enumerate(self._state.worktrees):
                git_data[wt.name] = (results[i * 2], results[i * 2 + 1])

        if self._active_worktree:
            try:
                wtc = self.query_one(f"#wtc-{self._active_worktree.name}", WorktreeTabContent)
                sidebar = wtc.query_one(SessionSidebar)
                gd = git_data.get(self._active_worktree.name)
                sidebar.show_worktree(
                    self._active_worktree,
                    states=self._cached_session_states,
                    git_status=gd[0] if gd else None,
                    git_dirty=gd[1] if gd else None,
                )
            except Exception:
                logger.debug("Failed to refresh active worktree sidebar", exc_info=True)

        for wt in self._state.worktrees:
            self._refresh_tab_label(wt, git_data=git_data.get(wt.name))

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _refresh_sidebar(self, wt: Worktree) -> None:
        session_names = [s.tmux_session_name for s in wt.sessions]
        states, status, dirty = await asyncio.gather(
            asyncio.to_thread(batch_detect_session_states, session_names) if session_names else asyncio.sleep(0, result={}),
            asyncio.to_thread(get_branch_status, wt.path, self._config.remote, self._config.main_branch),
            asyncio.to_thread(get_worktree_dirty, wt.path),
        )
        try:
            wtc = self.query_one(f"#wtc-{wt.name}", WorktreeTabContent)
            wtc.query_one(SessionSidebar).show_worktree(wt, states=states, git_status=status, git_dirty=dirty)
        except Exception:
            logger.debug("Failed to refresh sidebar", exc_info=True, extra={"worktree": wt.name})

    async def _refresh_git_ui(self, wt: Worktree) -> None:
        invalidate_git_cache(wt.path)
        status = await asyncio.to_thread(get_branch_status, wt.path, self._config.remote, self._config.main_branch)
        dirty = await asyncio.to_thread(get_worktree_dirty, wt.path)
        try:
            wtc = self.query_one(f"#wtc-{wt.name}", WorktreeTabContent)
            wtc.query_one(SessionSidebar)._refresh_git_status(wt, status=status, dirty=dirty)
        except Exception:
            logger.debug("Failed to refresh sidebar git status", exc_info=True, extra={"worktree": wt.name})
        self._refresh_tab_label(wt, git_data=(status, dirty))

    def _refresh_tab_label(self, wt: Worktree, git_data: tuple[dict, bool] | None = None) -> None:
        try:
            tabs = self.query_one("#tabs", TabbedContent)
            tab = tabs.get_tab(f"wt-{wt.name}")
            tab.label = self._tab_label(wt, git_data=git_data)
        except Exception:
            logger.debug("Failed to refresh tab label", exc_info=True, extra={"worktree": wt.name})

    # ── Git actions ───────────────────────────────────────────────────────────

    def _git_push(self, wt: Worktree) -> None:
        async def _push() -> None:
            try:
                repo = gitpython.Repo(wt.path)
                await asyncio.to_thread(repo.git.push, "-u", self._config.remote, wt.branch)
                self.app.notify(f"Pushed to {self._config.remote}")
            except gitpython.GitCommandError as e:
                self.app.notify(f"Push failed: {str(e.stderr or e)[:100]}", severity="error")
            await self._refresh_git_ui(wt)

        self.run_worker(_push, exclusive=False)

    def _git_pull(self, wt: Worktree) -> None:
        async def _pull() -> None:
            try:
                repo = gitpython.Repo(wt.path)
                await asyncio.to_thread(repo.git.pull, self._config.remote, self._config.main_branch)
                self.app.notify(f"Pulled latest from {self._config.main_branch}")
            except gitpython.GitCommandError as e:
                self.app.notify(f"Pull failed: {str(e.stderr or e)[:100]}", severity="error")
            await self._refresh_git_ui(wt)

        self.run_worker(_pull, exclusive=False)

    def _git_create_pr(self, wt: Worktree) -> None:
        async def _pr() -> None:
            result = await asyncio.to_thread(
                subprocess.run, ["gh", "auth", "status"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                self.app.notify("gh CLI not installed or not authenticated. Run: gh auth login", severity="error")
                return
            result = await asyncio.to_thread(
                subprocess.run, ["gh", "pr", "create", "--fill", "--head", wt.branch],
                cwd=wt.path, capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0:
                url = result.stdout.strip()
                webbrowser.open(url)
                self.app.notify(f"PR created: {url}")
            else:
                self.app.notify(f"PR failed: {(result.stderr or '')[:100]}", severity="error")

        self.run_worker(_pr, exclusive=False)

    def _git_commit(self, wt: Worktree) -> None:
        def handle_message(msg: str | None) -> None:
            if msg is None:
                return

            async def _commit() -> None:
                try:
                    repo = gitpython.Repo(wt.path)
                    await asyncio.to_thread(repo.git.add, "-u")
                    await asyncio.to_thread(repo.git.commit, "-m", msg)
                    self.app.notify("Committed")
                except gitpython.GitCommandError as e:
                    self.app.notify(f"Commit failed: {str(e.stderr or e)[:100]}", severity="error")
                await self._refresh_git_ui(wt)

            self.run_worker(_commit, exclusive=False)

        self.app.push_screen(CommitMessageScreen(self._config.commit_placeholder), callback=handle_message)
