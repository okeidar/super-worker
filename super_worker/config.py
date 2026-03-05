"""Configuration loading with auto-detection fallbacks.

Reads `.sw.toml` (project-level) and `~/.config/sw/config.toml` (global),
merges them, and fills missing values via git auto-detection.
"""

import hashlib
import tomllib
from pathlib import Path

import git as gitpython
from pydantic import BaseModel


class WorktreeConfig(BaseModel):
    prefix: str = ""
    branch_prefix: str = ""
    base_dir: str = ""


class EnvConfig(BaseModel):
    symlinks: list[str] = []
    copies: list[str] = []
    post_create_hook: str = ""


class GitConfig(BaseModel):
    main_branch: str = ""
    remote: str = ""


class UIConfig(BaseModel):
    commit_placeholder: str = ""
    name_placeholder: str = ""
    branch_placeholder: str = ""


class SWConfig(BaseModel):
    worktree: WorktreeConfig = WorktreeConfig()
    env: EnvConfig = EnvConfig()
    git: GitConfig = GitConfig()
    ui: UIConfig = UIConfig()


class ResolvedConfig(BaseModel):
    """Flat config with all values guaranteed filled."""

    repo_root: Path
    worktree_prefix: str
    branch_prefix: str
    base_dir: Path
    symlinks: list[str]
    copies: list[str]
    post_create_hook: str
    main_branch: str
    remote: str
    commit_placeholder: str
    name_placeholder: str
    branch_placeholder: str

    @property
    def state_hash(self) -> str:
        """Short hash of repo_root for per-repo state file naming."""
        return hashlib.sha256(str(self.repo_root).encode()).hexdigest()[:12]


def detect_repo_root(cwd: Path | str | None = None) -> Path:
    try:
        repo = gitpython.Repo(cwd or ".", search_parent_directories=True)
        # For linked worktrees, git-common-dir points to the main repo's .git/.
        # For the main repo itself it returns ".git" (relative).
        try:
            common_dir = repo.git.rev_parse("--git-common-dir")
            common_path = Path(common_dir)
            if not common_path.is_absolute():
                common_path = Path(repo.working_dir) / common_path
            return common_path.parent
        except Exception:
            return Path(repo.working_dir)
    except gitpython.InvalidGitRepositoryError:
        raise RuntimeError("Not inside a git repository")


def detect_remote(cwd: Path | str | None = None) -> str:
    try:
        repo = gitpython.Repo(cwd or ".", search_parent_directories=True)
        remotes = [r.name for r in repo.remotes]
        if not remotes:
            return "origin"
        return "origin" if "origin" in remotes else remotes[0]
    except Exception:
        return "origin"


def detect_main_branch(remote: str, cwd: Path | str | None = None) -> str:
    try:
        repo = gitpython.Repo(cwd or ".", search_parent_directories=True)
        ref = repo.git.symbolic_ref(f"refs/remotes/{remote}/HEAD")
        return ref.split("/")[-1]
    except gitpython.GitCommandError:
        pass
    # Fallback: check common branch names
    try:
        repo = gitpython.Repo(cwd or ".", search_parent_directories=True)
        for candidate in ("main", "master"):
            try:
                repo.git.rev_parse("--verify", f"refs/remotes/{remote}/{candidate}")
                return candidate
            except gitpython.GitCommandError:
                continue
    except Exception:
        pass
    return "main"


def save_project_config(repo_root: Path, config: SWConfig) -> Path:
    """Save project-level .sw.toml. Returns the path written."""
    path = repo_root / ".sw.toml"
    lines: list[str] = []
    for section_name in ("worktree", "env", "git", "ui"):
        section = getattr(config, section_name)
        section_lines: list[str] = []
        for field_name, field_info in type(section).model_fields.items():
            value = getattr(section, field_name)
            default = field_info.default
            if value != default:
                section_lines.append(f"{field_name} = {_toml_value(value)}")
        if section_lines:
            lines.append(f"[{section_name}]")
            lines.extend(section_lines)
            lines.append("")
    path.write_text("\n".join(lines) + "\n" if lines else "")
    return path


def _toml_value(value: object) -> str:
    """Format a Python value as TOML."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        items = ", ".join(f'"{_escape_toml_str(v)}"' for v in value)
        return f"[{items}]"
    if isinstance(value, str):
        return f'"{_escape_toml_str(value)}"'
    return str(value)


def _escape_toml_str(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def load_toml(path: Path) -> SWConfig:
    if not path.exists():
        return SWConfig()
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return SWConfig.model_validate(data)


def _merge_configs(project: SWConfig, global_: SWConfig) -> SWConfig:
    """Merge project over global. Non-empty project values win."""
    merged = SWConfig()
    for section in ("worktree", "env", "git", "ui"):
        proj_section = getattr(project, section)
        glob_section = getattr(global_, section)
        merged_section = getattr(merged, section)
        for field_name in type(proj_section).model_fields:
            proj_val = getattr(proj_section, field_name)
            glob_val = getattr(glob_section, field_name)
            default_val = type(merged_section).model_fields[field_name].default
            # Use project value if set (non-default), else global, else default
            if proj_val != default_val:
                setattr(merged_section, field_name, proj_val)
            elif glob_val != default_val:
                setattr(merged_section, field_name, glob_val)
    return merged


def load_config(repo_path: Path | str | None = None) -> ResolvedConfig:
    """Load and resolve configuration with auto-detection fallbacks.

    Args:
        repo_path: Optional path to a git repo. If None, auto-detects from CWD.
    """
    cwd = Path(repo_path) if repo_path else None
    repo_root = detect_repo_root(cwd)
    global_config_path = Path.home() / ".config" / "sw" / "config.toml"
    project_config_path = repo_root / ".sw.toml"

    global_cfg = load_toml(global_config_path)
    project_cfg = load_toml(project_config_path)
    merged = _merge_configs(project_cfg, global_cfg)

    remote = merged.git.remote or detect_remote(repo_root)
    main_branch = merged.git.main_branch or detect_main_branch(remote, repo_root)
    repo_name = repo_root.name
    branch_prefix = merged.worktree.branch_prefix or "sw-"

    return ResolvedConfig(
        repo_root=repo_root,
        worktree_prefix=merged.worktree.prefix or repo_name,
        branch_prefix=branch_prefix,
        base_dir=Path(merged.worktree.base_dir) if merged.worktree.base_dir else repo_root.parent,
        symlinks=merged.env.symlinks or [".venv", ".claude"],
        copies=merged.env.copies or [],
        post_create_hook=merged.env.post_create_hook,
        main_branch=main_branch,
        remote=remote,
        commit_placeholder=merged.ui.commit_placeholder or "Brief description of changes",
        name_placeholder=merged.ui.name_placeholder or "feature-name",
        branch_placeholder=merged.ui.branch_placeholder or f"{branch_prefix}<name>",
    )
