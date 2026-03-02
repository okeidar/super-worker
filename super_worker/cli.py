import shutil
import sys

import click

from super_worker.config import load_config, load_toml, save_project_config
from super_worker.services.state import load_state, remove_worktree_from_state, save_state, update_projects_registry
from super_worker.services.tmux import create_session, is_session_alive, kill_all_sessions
from super_worker.services.worktree import (
    BranchExistsError,
    create_worktree,
    get_branch_status,
    get_worktree_dirty,
    remove_worktree,
)


def _check_prerequisites() -> None:
    """Verify tmux and claude CLI are available, exit with helpful message if not."""
    missing = []
    if not shutil.which("tmux"):
        missing.append("tmux — install via: brew install tmux (macOS) or apt install tmux (Linux)")
    if not shutil.which("claude"):
        missing.append("claude — install via: npm install -g @anthropic-ai/claude-code")
    if missing:
        click.echo("Missing required tools:\n", err=True)
        for m in missing:
            click.echo(f"  • {m}", err=True)
        click.echo("\nSee: https://github.com/okeidar/super-worker#prerequisites", err=True)
        sys.exit(1)


def _require_git_repo() -> None:
    """Exit with a clear message if not inside a git repository."""
    from super_worker.config import detect_repo_root
    try:
        detect_repo_root()
    except RuntimeError as e:
        click.echo(f"Error: {e}", err=True)
        click.echo("Tip: run 'sw' without a subcommand from any directory to open the TUI.", err=True)
        sys.exit(1)


@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Super Worker — Claude Code Instance Manager for Git Worktrees."""
    _check_prerequisites()
    if ctx.invoked_subcommand is None:
        # Lazy import: SuperWorkerApp pulls in Textual, which is slow to load.
        # CLI-only commands (new, list, cleanup, config) skip this cost.
        from super_worker.app import SuperWorkerApp

        app = SuperWorkerApp()
        app.run()


@cli.command()
@click.argument("name")
@click.option("--branch", "-b", default=None, help="Branch name (defaults to name)")
@click.option("--prompt", "-p", default=None, help="Initial prompt or skill for Claude Code")
@click.option("--skip-permissions", "-s", is_flag=True, help="Launch Claude Code with --dangerously-skip-permissions")
def new(name: str, branch: str | None, prompt: str | None, skip_permissions: bool) -> None:
    """Create a new worktree and optionally launch a Claude Code session."""
    _require_git_repo()
    config = load_config()
    state = load_state(config)
    update_projects_registry(config)

    if state.get_worktree(name):
        click.echo(f"Worktree '{name}' already exists.", err=True)
        raise SystemExit(1)

    try:
        wt = create_worktree(config, name, branch, worktree_index=len(state.worktrees))
    except BranchExistsError as e:
        if click.confirm(f"Branch '{e.branch}' already exists. Use it?"):
            wt = create_worktree(config, name, branch, use_existing_branch=True, worktree_index=len(state.worktrees))
        else:
            click.echo("Aborted.", err=True)
            raise SystemExit(1)
    except FileExistsError as e:
        click.echo(str(e), err=True)
        raise SystemExit(1)

    state.worktrees.append(wt)
    click.echo(f"Created worktree: {wt.path} (branch: {wt.branch})")

    if prompt:
        session = create_session(wt, prompt=prompt, label=prompt, skip_permissions=skip_permissions)
        wt.sessions.append(session)
        click.echo(f"Launched session: {session.tmux_session_name} ({session.label})")

    save_state(state, config)


@cli.command("add")
@click.argument("worktree_name")
@click.option("--prompt", "-p", default=None, help="Initial prompt or skill for Claude Code")
@click.option("--label", "-l", default=None, help="Session label")
@click.option("--skip-permissions", "-s", is_flag=True, help="Launch Claude Code with --dangerously-skip-permissions")
def add_session(worktree_name: str, prompt: str | None, label: str | None, skip_permissions: bool) -> None:
    """Add a new CC session to an existing worktree."""
    _require_git_repo()
    config = load_config()
    state = load_state(config)
    wt = state.get_worktree(worktree_name)
    if not wt:
        click.echo(f"Worktree '{worktree_name}' not found.", err=True)
        raise SystemExit(1)

    session = create_session(wt, prompt=prompt, label=label, skip_permissions=skip_permissions)
    wt.sessions.append(session)
    click.echo(f"Launched session: {session.tmux_session_name} ({session.label})")
    save_state(state, config)


@cli.command("list")
def list_cmd() -> None:
    """List all worktrees and their sessions."""
    _require_git_repo()
    config = load_config()
    state = load_state(config)
    if not state.worktrees:
        click.echo("No worktrees.")
        return

    for wt in state.worktrees:
        status = get_branch_status(wt.path, config.remote, config.main_branch)
        dirty = get_worktree_dirty(wt.path)
        dirty_marker = " *" if dirty else ""
        status_str = f"↑{status['ahead']} ↓{status['behind']}"
        click.echo(f"\n{wt.name} ({wt.branch}) [{status_str}]{dirty_marker}")
        click.echo(f"  path: {wt.path}")
        if not wt.sessions:
            click.echo("  (no sessions)")
        for s in wt.sessions:
            alive = "alive" if is_session_alive(s.tmux_session_name) else "exited"
            click.echo(f"  {s.label} [{alive}] — {s.tmux_session_name}")


@cli.command()
@click.argument("name")
@click.option("--force", "-f", is_flag=True, help="Force remove even with uncommitted changes")
def cleanup(name: str, force: bool) -> None:
    """Kill all sessions and remove a worktree."""
    _require_git_repo()
    config = load_config()
    state = load_state(config)
    wt = state.get_worktree(name)
    if not wt:
        click.echo(f"Worktree '{name}' not found.", err=True)
        raise SystemExit(1)

    kill_all_sessions(wt)
    click.echo(f"Killed {len(wt.sessions)} session(s).")

    try:
        remove_worktree(state, name, force=force)
        click.echo(f"Removed worktree: {wt.path}")
    except RuntimeError as e:
        click.echo(str(e), err=True)
        raise SystemExit(1)

    state = remove_worktree_from_state(state, name)
    save_state(state, config)


@cli.command()
@click.argument("key", required=False)
@click.argument("value", required=False)
def config(key: str | None, value: str | None) -> None:
    """View or edit project settings (.sw.toml).

    With no args: show current config.
    With KEY: show a specific value.
    With KEY VALUE: set a value (e.g. `sw config worktree.branch_prefix sc-`).
    """
    _require_git_repo()
    resolved = load_config()
    project_cfg = load_toml(resolved.repo_root / ".sw.toml")

    if key is None:
        # Show all resolved config
        click.echo(f"Project: {resolved.repo_root}")
        click.echo(f"Config:  {resolved.repo_root / '.sw.toml'}\n")
        click.echo(f"[worktree]")
        click.echo(f"  prefix        = {resolved.worktree_prefix}")
        click.echo(f"  branch_prefix = {resolved.branch_prefix}")
        click.echo(f"  base_dir      = {resolved.base_dir}")
        click.echo(f"\n[env]")
        click.echo(f"  symlinks         = {resolved.symlinks}")
        click.echo(f"  copies           = {resolved.copies}")
        click.echo(f"  post_create_hook = {resolved.post_create_hook or '(none)'}")
        click.echo(f"\n[git]")
        click.echo(f"  main_branch = {resolved.main_branch}")
        click.echo(f"  remote      = {resolved.remote}")
        click.echo(f"\n[ui]")
        click.echo(f"  commit_placeholder = {resolved.commit_placeholder}")
        click.echo(f"  name_placeholder   = {resolved.name_placeholder}")
        click.echo(f"  branch_placeholder = {resolved.branch_placeholder}")
        return

    if "." not in key:
        click.echo("Key must be section.field (e.g. worktree.branch_prefix)", err=True)
        raise SystemExit(1)

    section_name, field_name = key.split(".", 1)
    section = getattr(project_cfg, section_name, None)
    if section is None or field_name not in section.model_fields:
        click.echo(f"Unknown config key: {key}", err=True)
        raise SystemExit(1)

    if value is None:
        # Show specific value
        click.echo(getattr(section, field_name))
        return

    # Set value
    field_type = type(getattr(section, field_name))
    if field_type is list:
        parsed_value = [s.strip() for s in value.split(",") if s.strip()]
    else:
        parsed_value = value
    setattr(section, field_name, parsed_value)
    path = save_project_config(resolved.repo_root, project_cfg)
    click.echo(f"Set {key} = {parsed_value}")
    click.echo(f"Saved to {path}")
