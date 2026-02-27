# Super Worker

TUI and CLI for managing multiple Claude Code sessions across git worktrees.

## Features

- Create and manage git worktrees with isolated Claude Code sessions
- Terminal preview pane with live tmux capture
- Full attach mode for direct tmux interaction
- Session state detection (running, waiting for input, waiting for approval)
- Git operations (commit, push, pull, PR creation) per worktree
- Multi-project support with project switcher
- Configurable per-project settings via `.sw.toml`
- CLI commands for scripting and automation

## Prerequisites

- **macOS or Linux** (uses Unix file locking and tmux)
- Python 3.11+
- [tmux](https://github.com/tmux/tmux)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`claude`)

## Install

```bash
# Quick setup (installs dependencies + sw globally)
git clone https://github.com/okeidar/super-worker.git
cd super-worker
./setup.sh
```

Or manually with [pipx](https://pipx.pypa.io/) (recommended):

```bash
git clone https://github.com/okeidar/super-worker.git
pipx install -e ./super-worker
```

This makes `sw` available globally from any terminal, without activating a venv.

## Quick Start

Navigate to any git repository and run:

```bash
sw
```

This launches the TUI. From there:

1. **Ctrl+N** to create a new worktree (with optional initial prompt)
2. **Ctrl+S** to add another session to the current worktree
3. **Ctrl+A** to attach directly to the active tmux session
4. Click sessions in the sidebar to switch between them

## CLI

```bash
sw new my-feature --prompt "/plan"     # Create worktree + session
sw add my-feature --prompt "/execute"  # Add session to existing worktree
sw list                                # List all worktrees and sessions
sw cleanup my-feature                  # Kill sessions and remove worktree
sw config                              # Show current config
sw config worktree.branch_prefix sc-   # Set a config value
```

## Configuration

**No configuration is required.** Super Worker auto-detects your git remote, main branch, and repo structure. It works out of the box.

To customize behavior, use `Ctrl+E` in the TUI or the CLI:

```bash
sw config                              # Show current (auto-detected) config
sw config worktree.branch_prefix sc-   # Set a value
```

This creates a `.sw.toml` in your project root. You can also create one manually:

```toml
[worktree]
branch_prefix = "sw-"
base_dir = "/path/to/worktrees"

[env]
symlinks = [".venv", ".claude"]
copies = [".env"]
post_create_hook = "scripts/setup.sh"

[git]
main_branch = "main"
remote = "origin"
```

Global defaults can be set in `~/.config/sw/config.toml` (same format). Project settings override global.

## Keybindings

| Key | Action |
|---|---|
| Ctrl+N | Create new worktree |
| Ctrl+S | Add session to current worktree |
| Ctrl+R | Rename active session |
| Ctrl+A | Attach to active tmux session |
| Ctrl+T | Open session in external terminal |
| Ctrl+D | Delete current worktree |
| Ctrl+O | Switch project |
| Ctrl+E | Edit project settings |
| Ctrl+Q | Quit |
| x | Delete selected session (in sidebar) |

## Development

```bash
pip install -e ".[dev]"
pytest
```

If you installed via `pipx`, code changes in the repo take effect immediately (editable mode).

## License

MIT
