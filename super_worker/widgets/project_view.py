"""Per-project widget: worktree tabs, session management, git actions."""

import asyncio
import logging
import shlex
import subprocess
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
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
    ensure_default_worktree,
    remove_session_from_state,
    remove_worktree_from_state,
    save_state,
)
from super_worker.services.tmux import (
    SessionState,
    batch_check_alive,
    batch_detect_session_states,
    cleanup_state_file,
    create_session,
    enable_mouse,
    has_waiting_approval,
    kill_all_sessions,
    kill_session,
    open_external_terminal,
    read_all_state_files,
    read_state_file,
    verify_waiting_approval,
)
from super_worker.services.worktree import (
    BranchExistsError,
    create_worktree,
    get_branch_status,
    get_worktree_dirty,
    git_commit,
    git_create_pr,
    git_pull,
    git_push,
    invalidate_git_cache,
    list_local_branches,
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

    class AttentionChanged(Message):
        """Posted when the project's attention state (any session waiting approval) changes."""

        def __init__(self, path: str, needs_attention: bool) -> None:
            self.path = path
            self.needs_attention = needs_attention
            super().__init__()

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
        ensure_default_worktree(self._state, self._config)
        # TUI mode also needs at least one tmux session per worktree
        wt = self._state.get_worktree(DEFAULT_WORKTREE_NAME)
        if wt and not wt.sessions:
            session = create_session(wt)
            wt.sessions.append(session)
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
            wt = self._state.worktrees[0]
            self._active_worktree = wt
            if wt.sessions:
                self._active_session_name = wt.sessions[0].tmux_session_name

            async def _initial_refresh():
                await self._refresh_sidebar(wt)
                self._set_active_worktree(wt)
                self._start_state_watching()

            self.run_worker(_initial_refresh, exclusive=False)

    def _tab_label(self, wt: Worktree, git_data: tuple[dict, bool] | None = None) -> str:
        wt_states = {s.tmux_session_name: self._cached_session_states.get(s.tmux_session_name, SessionState.RUNNING) for s in wt.sessions}
        attention = " 🔔" if has_waiting_approval(wt_states) else ""
        if git_data is None:
            return f"{wt.name}{attention}"
        status, dirty = git_data
        dirty_marker = " *" if dirty else ""
        return f"{wt.name} (↑{status['ahead']} ↓{status['behind']}){dirty_marker}{attention}"

    def _update_app_subtitle(self, session_label: str | None = None) -> None:
        """Update the app subtitle to include the active session label."""
        try:
            base = str(self._config.repo_root)
            if session_label:
                self.app.sub_title = f"{base} · {session_label}"
            else:
                self.app.sub_title = base
        except Exception:
            pass

    def _start_state_watching(self) -> None:
        """Start kqueue watches on state files for ALL sessions in this project.

        Called once on mount and whenever sessions change. The active worktree's
        TerminalPane hosts the watchers for all sessions across all worktrees,
        so attention indicators update instantly for the entire project.
        """
        all_names = [s.tmux_session_name for wt in self._state.worktrees for s in wt.sessions]
        if not all_names or not self._active_worktree:
            return
        try:
            wtc = self.query_one(f"#wtc-{self._active_worktree.name}", WorktreeTabContent)
            terminal = wtc.query_one(TerminalPane)
            terminal.start_watching_states(all_names)
        except Exception:
            pass

    def pause_watching(self) -> None:
        """Pause terminal capture when this project becomes inactive.

        The kqueue state-file watchers keep running so attention indicators
        (bell icon) still update for background projects.
        """
        if not self._active_worktree:
            return
        try:
            wtc = self.query_one(f"#wtc-{self._active_worktree.name}", WorktreeTabContent)
            wtc.query_one(TerminalPane).pause_watching()
        except Exception:
            pass

    def resume_watching(self) -> None:
        """Resume terminal capture when this project becomes active again."""
        if not self._active_worktree:
            return
        try:
            wtc = self.query_one(f"#wtc-{self._active_worktree.name}", WorktreeTabContent)
            wtc.query_one(TerminalPane).resume_watching()
        except Exception:
            pass

    def _set_active_worktree(self, wt: Worktree) -> None:
        # Pause the old worktree's terminal captures (keeps content + state watches)
        old_wt = self._active_worktree
        if old_wt and old_wt.name != wt.name:
            try:
                old_wtc = self.query_one(f"#wtc-{old_wt.name}", WorktreeTabContent)
                old_wtc.query_one(TerminalPane).pause_watching()
            except Exception:
                pass
        self._active_worktree = wt
        if wt.sessions:
            first = wt.sessions[0]
            self._active_session_name = first.tmux_session_name
            self._activate_terminal(wt.name, first.tmux_session_name)
            self._update_app_subtitle(first.label)

    def _activate_terminal(self, wt_name: str, tmux_session_name: str) -> None:
        """Set the active session on a worktree's terminal pane.

        Uses call_after_refresh so it works even when the widget tree
        hasn't fully composed yet (e.g. initial mount race).
        """
        def _do_activate() -> None:
            try:
                wtc = self.query_one(f"#wtc-{wt_name}", WorktreeTabContent)
                wtc.query_one(SessionSidebar).select_session(tmux_session_name)
                terminal = wtc.query_one(TerminalPane)
                terminal.active_session = tmux_session_name
                terminal.resume_watching()
                terminal.focus()
            except Exception:
                pass
        self.call_after_refresh(_do_activate)

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        tab_id = event.pane.id
        if tab_id and tab_id.startswith("wt-"):
            name = tab_id[3:]
            wt = self._state.get_worktree(name)
            if wt:
                self._set_active_worktree(wt)

    def on_terminal_pane_state_changed(self, event: TerminalPane.StateChanged) -> None:
        """Session state changed (via kqueue on state file) — update UI instantly."""
        name = event.session_name
        new_state = read_state_file(name)
        old_state = self._cached_session_states.get(name)
        if new_state == old_state:
            return
        # Scope attention check to current sessions only — stale cache entries
        # for deleted sessions could keep the bell icon on indefinitely.
        current_names = {s.tmux_session_name for wt in self._state.worktrees for s in wt.sessions}
        current_states = {k: v for k, v in self._cached_session_states.items() if k in current_names}
        old_attention = has_waiting_approval(current_states)
        self._cached_session_states[name] = new_state
        current_states[name] = new_state
        new_attention = has_waiting_approval(current_states)
        if old_attention != new_attention:
            self.post_message(self.AttentionChanged(
                str(self._config.repo_root), new_attention
            ))
        for wt in self._state.worktrees:
            self._refresh_tab_label(wt, git_data=None)
        wt = self._active_worktree
        if wt:
            try:
                wtc = self.query_one(f"#wtc-{wt.name}", WorktreeTabContent)
                sidebar = wtc.query_one(SessionSidebar)
                sidebar.show_worktree(wt, states=self._cached_session_states)
            except Exception:
                pass

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
        self._update_app_subtitle(event.session.label)

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
            self._update_app_subtitle(next_session.label)
        else:
            self._update_app_subtitle()

        self.app.notify(f"Deleted session: {session.label}")
        await asyncio.to_thread(kill_session, tmux_name)
        cleanup_state_file(tmux_name)
        self._start_state_watching()  # Update watch list
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
        branches = list_local_branches(self._config.repo_root)

        def handle_result(result: tuple[str, str | None, str | None, bool, bool, bool] | None) -> None:
            if result is None:
                return
            name, branch, prompt, detach, skip_perms, use_existing = result
            if self._state.get_worktree(name):
                self.app.notify(f"Worktree '{name}' already exists", severity="error")
                return
            self._create_worktree(name, prompt, branch=branch, use_existing_branch=use_existing, detach=detach, skip_permissions=skip_perms)

        self.app.push_screen(NewWorktreeScreen(self._config, branches=branches), callback=handle_result)

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
            if prompt or skip_permissions:
                session = await asyncio.to_thread(
                    create_session, wt, prompt=prompt, label=prompt,
                    skip_permissions=skip_permissions,
                )
                wt.sessions.append(session)
            await asyncio.to_thread(save_state, self._state, self._config)
            await self._add_worktree_tab(wt)
            self._start_state_watching()  # Watch new worktree's sessions
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
                self._activate_terminal(wt.name, session.tmux_session_name)
                self._start_state_watching()  # Watch new session's state file
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
        # Refresh sidebar so session list and selection are restored after suspend
        wt = self._active_worktree
        if wt:
            self.run_worker(self._refresh_sidebar(wt), exclusive=False)

    def do_open_terminal(self) -> None:
        if not self._active_session_name:
            self.app.notify("No active session to open", severity="warning")
            return
        session_name = self._active_session_name

        async def _open() -> None:
            await asyncio.to_thread(enable_mouse, session_name)
            opened = await asyncio.to_thread(open_external_terminal, session_name)
            if not opened:
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

        def handle_confirm(del_branch: bool | None) -> None:
            if del_branch is None:
                return

            async def _delete() -> None:
                target = self._state.get_worktree(wt_name)
                if not target:
                    return
                try:
                    await asyncio.to_thread(kill_all_sessions, target)
                    await asyncio.to_thread(
                        remove_worktree, self._state, wt_name,
                        force=True, delete_branch=del_branch,
                        remote=self._config.remote,
                    )
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

        self.app.push_screen(ConfirmDeleteScreen(wt.name, wt.branch), callback=handle_confirm)

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

    async def check_attention(self) -> None:
        """Lightweight state-only check for non-active projects.

        Reads state files (no subprocess calls) for instant attention detection.
        """
        old_attention = has_waiting_approval(self._cached_session_states)
        all_session_names = [s.tmux_session_name for wt in self._state.worktrees for s in wt.sessions]
        if all_session_names:
            self._cached_session_states = read_all_state_files(all_session_names)
        else:
            self._cached_session_states = {}
        new_attention = has_waiting_approval(self._cached_session_states)
        if old_attention != new_attention:
            self.post_message(self.AttentionChanged(
                str(self._config.repo_root), new_attention
            ))

    async def periodic_refresh(self) -> None:
        """Fetch git data and detect dead sessions. Called by app timer.

        State detection is event-driven via kqueue on state files (see
        on_terminal_pane_state_changed). This method only handles:
        - Git status (no event source, must poll)
        - Dead session detection (lightweight alive check, no show_environment)
        - Syncing state cache from state files for sessions without kqueue watches
        """
        all_session_names = [s.tmux_session_name for wt in self._state.worktrees for s in wt.sessions]
        if all_session_names:
            # Lightweight: single list-sessions + pane_dead check (no show_environment)
            dead_names = await asyncio.to_thread(batch_check_alive, all_session_names)
            # Read state from files (no subprocess) for live sessions
            file_states = read_all_state_files(all_session_names)

            old_attention = has_waiting_approval(self._cached_session_states)
            # Rebuild cache from current sessions only — prune stale entries
            # for deleted sessions whose last state might have been waiting_approval.
            new_cache: dict[str, SessionState] = {}
            for name in all_session_names:
                if name in dead_names:
                    new_cache[name] = SessionState.DEAD
                else:
                    new_cache[name] = file_states.get(name, SessionState.UNKNOWN)
            self._cached_session_states = new_cache

            # Cross-check sessions showing waiting_approval — state files can
            # be stale for sessions started before the latest hook was installed.
            suspect = [n for n, s in new_cache.items() if s == SessionState.WAITING_APPROVAL]
            if suspect:
                corrections = await asyncio.to_thread(verify_waiting_approval, suspect)
                for name, real_state in corrections.items():
                    self._cached_session_states[name] = real_state

            new_attention = has_waiting_approval(self._cached_session_states)
            if old_attention != new_attention:
                self.post_message(self.AttentionChanged(
                    str(self._config.repo_root), new_attention
                ))

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
            err = await asyncio.to_thread(git_push, wt.path, self._config.remote, wt.branch)
            if err:
                self.app.notify(f"Push failed: {err[:100]}", severity="error")
            else:
                self.app.notify(f"Pushed to {self._config.remote}")
            await self._refresh_git_ui(wt)

        self.run_worker(_push, exclusive=False)

    def _git_pull(self, wt: Worktree) -> None:
        async def _pull() -> None:
            err = await asyncio.to_thread(git_pull, wt.path, self._config.remote, self._config.main_branch)
            if err:
                self.app.notify(f"Pull failed: {err[:100]}", severity="error")
            else:
                self.app.notify(f"Pulled latest from {self._config.main_branch}")
            await self._refresh_git_ui(wt)

        self.run_worker(_pull, exclusive=False)

    def _git_create_pr(self, wt: Worktree) -> None:
        async def _pr() -> None:
            ok, result = await asyncio.to_thread(git_create_pr, wt.path, wt.branch)
            if ok:
                self.app.notify(f"PR created: {result}")
            else:
                self.app.notify(f"PR failed: {result[:100]}", severity="error")

        self.run_worker(_pr, exclusive=False)

    def _git_commit(self, wt: Worktree) -> None:
        def handle_message(msg: str | None) -> None:
            if msg is None:
                return

            async def _commit() -> None:
                err = await asyncio.to_thread(git_commit, wt.path, msg)
                if err:
                    self.app.notify(f"Commit failed: {err[:100]}", severity="error")
                else:
                    self.app.notify("Committed")
                await self._refresh_git_ui(wt)

            self.run_worker(_commit, exclusive=False)

        self.app.push_screen(CommitMessageScreen(self._config.commit_placeholder), callback=handle_message)
