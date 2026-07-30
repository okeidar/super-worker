# Super Worker

TUI and CLI for managing multiple Claude Code sessions across git worktrees.

## Features

- **Multi-worktree management** — create isolated worktrees with their own Claude Code sessions
- **Live terminal preview** — interactive preview of each session with a live cursor, full color (including diff backgrounds), and follow-tail scrolling; the tmux window is resized to match the preview so wrapping is exact
- **Full attach mode** — press Ctrl+A to drop into the tmux session directly (mouse, alt-screen apps)
- **Session state indicators** — live dots show running, waiting for input, or waiting for approval
- **Attention alerts** — appears on worktree tabs and project bar when a session needs approval
- **Multi-project support** — switch between repos with a project drawer or docked tab bar
- **Terminal sessions** — open plain shell sessions alongside Claude Code
- **Git operations** — commit, push, pull, and create PRs per worktree
- **Crash recovery** — dead sessions are automatically respawned on startup with `--continue`
- **Fast mode** — native tmux panes with zero rendering overhead (`sw --fast`)
- **Configurable** — per-project `.sw.toml` with auto-detection of remote, branch, and repo structure
- **CLI** — script worktree and session management from the command line

## Prerequisites

- **macOS** (uses kqueue for file watching and tmux)
- Python 3.11+
- [tmux](https://github.com/tmux/tmux)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`claude`)
- [GitHub CLI](https://cli.github.com/) (`gh`) — optional, for PR creation

## Install

```bash
git clone https://github.com/okeidar/super-worker.git
cd super-worker
./setup.sh
```

The setup script auto-detects your package manager (brew/apt/dnf/pacman), installs tmux if needed, and registers `sw` globally via pipx.

Or manually with [pipx](https://pipx.pypa.io/):

```bash
git clone https://github.com/okeidar/super-worker.git
pipx install -e ./super-worker
```

This makes `sw` available globally from any terminal.

## Quick Start

Navigate to any git repository and run:

```bash
sw           # TUI mode
sw --fast    # Fast mode (native tmux panes)
```

### TUI Mode

1. **Ctrl+N** — create a new worktree (with optional branch, prompt, and skip-permissions)
2. **Ctrl+S** — add a session to the current worktree
3. **Ctrl+A** — attach directly to the active tmux session
4. Click sessions in the sidebar to switch between them

### Fast Mode

All actions through one menu: **Ctrl+B, Space**.

## TUI Keybindings

| Key | Action |
|---|---|
| Ctrl+N | Create new worktree |
| Ctrl+S | Add session to current worktree |
| Ctrl+A | Attach to active tmux session |
| Ctrl+T | Open terminal session |
| F2 | Rename active session |
| Ctrl+R | Claude Code transcript view (browse full conversation history) |
| Ctrl+D | Delete current worktree |
| Ctrl+O | Toggle project drawer |
| Ctrl+Shift+Left/Right | Switch between projects |
| Ctrl+E | Edit project settings |
| Ctrl+Q | Quit |
| x | Delete selected session (in sidebar) |

**In the project drawer:**

| Key | Action |
|---|---|
| p | Dock drawer as tab bar |
| Del | Remove project from registry |
| Esc | Close drawer |

## Fast Mode

Fast mode runs sessions as native tmux panes — no TUI rendering overhead, direct terminal interaction.

```bash
sw --fast
```

| Key | Action |
|---|---|
| Ctrl+B, Space | Master menu (worktrees, sessions, git, settings) |
| Ctrl+B, g | Git actions menu |
| Ctrl+B, n / p | Switch between worktree tabs |

### Master Menu Actions

**Worktrees:** new worktree, delete worktree
**Sessions:** new session (split pane), kill pane, rename session, resume dead pane
**Git:** commit, push, pull, open PR
**Other:** switch project, open in terminal, edit settings, help

Sessions with previous conversations resume automatically with `--continue` on relaunch.

Window titles show branch status: `worktree (branch ↑ahead↓behind)` with `*` for dirty and `!` for sessions needing approval.

## Worktree Management

When creating a worktree you can:

- **Create a new branch** (default) — auto-named with a configurable prefix
- **Use an existing branch** — check "Use existing branch" and provide the branch name
- **Specify a branch name** — use `--branch` / `-b` in the CLI
- **Detached HEAD** — create a worktree with no branch

When deleting a worktree, you can optionally delete the branch (both local and remote).

## Session Types

- **Claude Code** — runs `claude` with an optional prompt (e.g., `/plan`). Supports `--dangerously-skip-permissions` via the skip-permissions checkbox/flag and `--continue` for resuming.
- **Terminal** — plain shell session for running commands, git operations, etc.

### Session Options

- **Prompt** — initial prompt sent to Claude Code on launch
- **Label** — custom display name for the session
- **Skip permissions** — launches Claude with `--dangerously-skip-permissions`

## Session States

Each session shows a colored dot indicating its state:

- **Running** (green) — session executing normally
- **Waiting for input** (yellow) — session blocked, waiting for user input
- **Waiting for approval** (magenta) — session needs user approval
- **Dead** (red) — session exited (auto-recovered on startup)

State detection uses a Claude Code hook (`sw-hook.sh`) that's automatically installed. In fast mode, pane border titles also update with state icons.

## Git Actions

Per-worktree git operations available from the TUI sidebar or fast mode menu:

- **Commit** — stages all changes (including new files, respecting `.gitignore`; sw-managed symlinks/copies like `.venv`/`.env` are never staged) and commits with a message
- **Push** — pushes to remote
- **Pull** — pulls from the main branch
- **Open PR** — creates a pull request via `gh pr create`

The sidebar also shows branch ahead/behind status and dirty working tree indicators.

## Multi-Project Support

- **Ctrl+O** opens the project drawer to switch repos
- Projects can be docked as a tab bar above worktree tabs (press `p` in the drawer)
- Attention indicators show across all open projects when sessions need approval
- State persists per-project across sessions
- **Del** removes a project from the registry

## CLI

```bash
sw                                     # Launch TUI
sw --fast                              # Launch fast mode (native tmux panes)
sw new my-feature                      # Create worktree with auto-named branch
sw new my-feature -b existing-branch   # Create worktree on existing branch
sw new my-feature -p "/plan"           # Create worktree + session with prompt
sw new my-feature -s                   # Create worktree with skip-permissions
sw add my-feature -p "/execute"        # Add session to existing worktree
sw add my-feature -l "tests" -s        # Add labeled session with skip-permissions
sw list                                # List worktrees and sessions
sw cleanup my-feature                  # Kill sessions and remove worktree
sw cleanup my-feature -f               # Force remove even with uncommitted changes
sw config                              # Show current config
sw config worktree.branch_prefix sc-   # Set a config value
```

## Configuration

**No configuration is required.** Super Worker auto-detects your git remote, main branch, and repo structure.

To customize, use `Ctrl+E` in the TUI or the CLI:

```bash
sw config worktree.branch_prefix sc-
```

This creates a `.sw.toml` in your project root:

```toml
[worktree]
prefix = "repo-name"
branch_prefix = "sw-"
base_dir = "/path/to/worktrees"

[env]
symlinks = [".venv", ".claude"]
copies = [".env"]
post_create_hook = "scripts/setup.sh"

[git]
main_branch = "main"
remote = "origin"

[ui]
commit_placeholder = "Brief description of changes"
name_placeholder = "worktree name"
branch_placeholder = "branch name"
```

Global defaults go in `~/.config/sw/config.toml` (same format). Project settings override global.

## Development

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT
