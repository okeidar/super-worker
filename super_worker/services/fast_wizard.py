"""Interactive wizards for fast mode, run inside tmux display-popup.

Each wizard collects input via simple stdin prompts (no Textual dependency),
then calls the same backend services as the TUI mode.
"""

import re
import subprocess
from pathlib import Path

from super_worker.config import load_config
from super_worker.constants import DEFAULT_WORKTREE_NAME, format_pane_title
from super_worker.services.fast_ui import (
    add_pane_to_window,
    create_worktree_window,
    find_window_for_worktree,
    make_fast_session,
    resolve_target,
    resolve_window_ref,
    worktree_name_from_window,
)
from super_worker.services.state import load_state, remove_worktree_from_state, save_state
from super_worker.services.tmux import _get_server
from super_worker.services.worktree import BranchExistsError, create_worktree, remove_worktree


def _resolve_host(host_ref: str) -> str:
    """Resolve a --session id ($N) or legacy session name to the host name."""
    info = resolve_target(host_ref) if host_ref.startswith("$") else None
    return info["session_name"] if info else host_ref


def _load_worktree_for_window(window_ref: str):
    """Load config, state, and worktree for a window reference (@id or name).

    The project is located from the window's pane cwd when available, so the
    wizard works regardless of the popup's working directory (tmux popups do
    not run in the project directory).

    Returns (config, state, worktree) or prints error and returns None.
    """
    wt_name, ctx_path = resolve_window_ref(window_ref)
    try:
        config = load_config(Path(ctx_path)) if ctx_path else load_config()
    except RuntimeError as e:
        print(f"  {e}")
        input("  Press Enter to close...")
        return None
    state = load_state(config)
    wt = state.get_worktree(wt_name)
    if not wt:
        print(f"  Worktree '{wt_name}' not found.")
        input("  Press Enter to close...")
        return None
    return config, state, wt


def _wizard_header(title: str) -> None:
    """Print a consistent wizard header."""
    print(f"\n  {title}")
    print("  " + "\u2500" * 35)


def wizard_new_worktree(host_session: str) -> None:
    """Interactive prompt for creating a new worktree."""
    host_session = _resolve_host(host_session)
    _wizard_header("New Worktree")

    name = input("  Name: ").strip()
    if not name or not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
        print("  Invalid name. Letters, digits, hyphens, underscores only.")
        input("  Press Enter to close...")
        return

    # Locate the project from the host session's pane cwd, not the popup cwd.
    info = resolve_target(host_session)
    try:
        config = load_config(Path(info["pane_path"])) if info else load_config()
    except RuntimeError as e:
        print(f"  {e}")
        input("  Press Enter to close...")
        return
    branch_default = f"{config.branch_prefix}{name}"
    branch = input(f"  Branch ({branch_default}): ").strip() or None
    prompt = input("  Prompt (optional): ").strip() or None
    detach = input("  Detach HEAD? [y/N]: ").strip().lower() == "y"
    skip_perms = input("  Skip permissions? [y/N]: ").strip().lower() == "y"

    state = load_state(config)
    if state.get_worktree(name):
        print(f"  Worktree '{name}' already exists.")
        input("  Press Enter to close...")
        return

    try:
        wt = create_worktree(
            config, name, branch=branch, detach=detach,
            worktree_index=len(state.worktrees),
        )
    except BranchExistsError as e:
        use = input(f"  Branch '{e.branch}' exists. Use it? [Y/n]: ").strip().lower()
        if use == "n":
            return
        try:
            wt = create_worktree(
                config, name, branch=branch, use_existing_branch=True,
                detach=detach, worktree_index=len(state.worktrees),
            )
        except Exception as e:  # noqa: F841 — retry failed; show it, don't traceback
            print(f"  Error: {e}")
            input("  Press Enter to close...")
            return
    except Exception as e:
        print(f"  Error: {e}")
        input("  Press Enter to close...")
        return

    state.worktrees.append(wt)

    session = make_fast_session(
        host_session, label=prompt or "session 0",
        prompt=prompt, skip_permissions=skip_perms,
    )

    server = _get_server()
    host = server.sessions.get(session_name=host_session)
    window, pane_id = create_worktree_window(host, wt, session, host_session)
    session.tmux_pane_id = pane_id
    wt.sessions.append(session)
    save_state(state, config)
    print(f"  Created worktree: {name}")


def wizard_new_session(host_session: str, window_ref: str) -> None:
    """Interactive prompt for adding a session to the current worktree window."""
    # New bindings pass only --window @id; derive the host session from it.
    if window_ref.startswith("@"):
        info = resolve_target(window_ref)
        if info:
            host_session = info["session_name"]

    _wizard_header("New Session")

    type_input = input("  Type [1=Claude, 2=Terminal] (1): ").strip()
    session_type = "terminal" if type_input == "2" else "claude"

    if session_type == "claude":
        prompt = input("  Prompt (optional): ").strip() or None
        skip_perms = input("  Skip permissions? [y/N]: ").strip().lower() == "y"
    else:
        prompt, skip_perms = None, False

    label = input("  Label (optional): ").strip() or None

    result = _load_worktree_for_window(window_ref)
    if not result:
        return
    config, state, wt = result

    session = make_fast_session(
        host_session,
        label=label or f"session {len(wt.sessions)}",
        session_type=session_type,
        prompt=prompt,
        skip_permissions=skip_perms,
    )

    server = _get_server()
    host = server.sessions.get(session_name=host_session)
    window = find_window_for_worktree(host, wt.name)
    if not window:
        print(f"  Window for worktree '{wt.name}' not found.")
        input("  Press Enter to close...")
        return

    pane_id = add_pane_to_window(window, wt, session, host_session)
    session.tmux_pane_id = pane_id
    wt.sessions.append(session)
    save_state(state, config)
    print(f"  Created session: {session.label}")


def wizard_rename_session(pane_ref: str) -> None:
    """Prompt for a new label and rename the session owning this pane.

    Replaces the old tmux command-prompt flow, which interpolated raw user
    input into a shell string (an apostrophe broke it; worse was possible).
    """
    info = resolve_target(pane_ref)
    if not info:
        print("  Pane not found.")
        input("  Press Enter to close...")
        return

    _wizard_header("Rename Session")
    label = input("  New label: ").strip()
    if not label:
        return

    try:
        config = load_config(Path(info["pane_path"]))
    except RuntimeError as e:
        print(f"  {e}")
        input("  Press Enter to close...")
        return
    state = load_state(config)
    match = state.find_session_by_pane_id(info["pane_id"])
    if not match:
        print("  Session not found in state.")
        input("  Press Enter to close...")
        return
    _, session = match
    session.label = label
    save_state(state, config)
    _get_server().cmd(
        "select-pane", "-t", info["pane_id"],
        "-T", format_pane_title(label, session.session_type),
    )
    print(f"  Renamed to: {label}")


def wizard_delete_worktree(host_session: str, window_ref: str) -> None:
    """Confirm and delete a worktree, optionally deleting the branch."""
    wt_name, _ = resolve_window_ref(window_ref)
    if window_ref.startswith("@"):
        info = resolve_target(window_ref)
        if info:
            host_session = info["session_name"]

    if wt_name == DEFAULT_WORKTREE_NAME:
        print("  Cannot delete the main worktree.")
        input("  Press Enter to close...")
        return

    result = _load_worktree_for_window(window_ref)
    if not result:
        return
    config, state, wt = result

    _wizard_header(f"Delete Worktree: {wt_name}")
    print(f"  Branch: {wt.branch}")
    print(f"  Path:   {wt.path}")
    print(f"  Sessions: {len(wt.sessions)}")
    print()

    confirm = input("  Delete this worktree? [y/N]: ").strip().lower()
    if confirm != "y":
        return

    del_branch = False
    if wt.branch and wt.branch != "(detached)":
        del_branch = input(f"  Also delete branch '{wt.branch}'? [y/N]: ").strip().lower() == "y"

    # Kill the tmux window (kills all panes in it)
    server = _get_server()
    try:
        host = server.sessions.get(session_name=host_session)
        window = find_window_for_worktree(host, wt_name)
        if window:
            window.kill()
    except Exception:
        pass

    try:
        remove_worktree(
            state, wt_name, force=True,
            delete_branch=del_branch, remote=config.remote,
        )
    except Exception as e:
        print(f"  Warning: {e}")

    state = remove_worktree_from_state(state, wt_name)
    save_state(state, config)
    print(f"  Deleted worktree: {wt_name}")
    if del_branch:
        # Branch deletion failures are logged, not raised — don't claim success.
        print(f"  Requested deletion of branch: {wt.branch}")


def wizard_git_commit(window_ref: str) -> None:
    """Prompt for commit message and commit."""
    from super_worker.services.worktree import git_commit

    result = _load_worktree_for_window(window_ref)
    if not result:
        return
    config, state, wt = result

    _wizard_header(f"Git Commit \u2014 {wt.name}")

    msg = input(f"  Message ({config.commit_placeholder}): ").strip()
    if not msg:
        print("  No message provided.")
        input("  Press Enter to close...")
        return

    exclude = list(config.copies) + list(config.symlinks)
    err = git_commit(wt.path, msg, exclude)
    print(f"  Commit failed: {err}" if err else "  Committed.")
    input("  Press Enter to close...")


def wizard_switch_project() -> None:
    """Show project list with options to switch, open by path, or remove."""
    from super_worker.services.state import load_projects_registry, remove_from_projects_registry

    projects = load_projects_registry()

    _wizard_header("Switch Project")
    if projects:
        for i, p in enumerate(projects, 1):
            print(f"  {i}. {Path(p).name}  ({p})")
    else:
        print("  (no registered projects)")
    print()
    print("  p = open by path | d = remove from list")

    choice = input("\n  Select: ").strip().lower()

    if choice == "p":
        path = input("  Path: ").strip()
        if not path:
            return
        path = str(Path(path).expanduser().resolve())
        if not Path(path).is_dir():
            print(f"  Not a directory: {path}")
            input("  Press Enter to close...")
            return
        subprocess.run(["sw", "--fast"], cwd=path)
    elif choice == "d":
        if not projects:
            print("  Nothing to remove.")
            input("  Press Enter to close...")
            return
        idx_str = input(f"  Remove which? (1-{len(projects)}): ").strip()
        try:
            idx = int(idx_str) - 1
            if 0 <= idx < len(projects):
                removed = projects[idx]
                remove_from_projects_registry(removed)
                print(f"  Removed: {removed}")
            else:
                print("  Invalid selection.")
        except ValueError:
            print("  Invalid selection.")
        input("  Press Enter to close...")
    else:
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(projects):
                path = projects[idx]
                subprocess.run(["sw", "--fast"], cwd=path)
            else:
                print("  Invalid selection.")
                input("  Press Enter to close...")
        except ValueError:
            print("  Invalid selection.")
            input("  Press Enter to close...")
