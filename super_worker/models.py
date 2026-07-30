from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class Session(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: uuid4().hex[:8])
    tmux_session_name: str
    tmux_pane_id: str | None = None  # e.g., "%5" — set in fast mode only
    label: str
    session_type: str = Field(default="claude")
    initial_prompt: str | None = None
    skip_permissions: bool = False
    # Claude Code conversation id (set via `claude --session-id`). Lets recovery
    # resume THIS session's own conversation (`--resume <id>`) instead of the
    # blunt `--continue`, which always reopens the single most-recent one.
    claude_session_id: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class Worktree(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    path: str
    branch: str
    sessions: list[Session] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class AppState(BaseModel):
    model_config = ConfigDict(extra="ignore")

    repo_root: str
    worktree_base: str
    worktrees: list[Worktree] = Field(default_factory=list)
    ui_mode: str = "tui"  # "tui" or "fast"

    def get_worktree(self, name: str) -> Worktree | None:
        for wt in self.worktrees:
            if wt.name == name:
                return wt
        return None

    def find_session_by_pane_id(self, pane_id: str) -> tuple["Worktree", "Session"] | None:
        """Find a session across all worktrees by its tmux pane ID."""
        for wt in self.worktrees:
            for s in wt.sessions:
                if s.tmux_pane_id == pane_id:
                    return wt, s
        return None
