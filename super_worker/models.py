from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class Session(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: uuid4().hex[:8])
    tmux_session_name: str
    label: str
    session_type: str = Field(default="claude")
    initial_prompt: str | None = None
    skip_permissions: bool = False
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

    def get_worktree(self, name: str) -> Worktree | None:
        for wt in self.worktrees:
            if wt.name == name:
                return wt
        return None
