"""Install/uninstall Claude Code hooks for session state detection."""

import importlib.resources
import json
import logging
import shutil
import stat
from pathlib import Path

from super_worker.constants import SESSION_STATES_DIR, STATE_DIR

logger = logging.getLogger(__name__)

_HOOK_SCRIPT_NAME = "sw-hook.sh"
_HOOK_DEST = STATE_DIR / _HOOK_SCRIPT_NAME
_CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"


def _get_hook_source() -> Path | None:
    """Locate the bundled hook script using importlib.resources."""
    try:
        ref = importlib.resources.files("super_worker.scripts").joinpath(_HOOK_SCRIPT_NAME)
        # Materialize to a real path (works for both installed and editable installs)
        with importlib.resources.as_file(ref) as path:
            return Path(path) if path.exists() else None
    except Exception:
        return None

# Marker prefix used to identify our hooks in settings.json
_HOOK_MARKER = "sw-hook.sh"


def _build_hooks() -> dict[str, list[dict]]:
    """Build hook configurations using the current _HOOK_DEST path.

    State model (drives the sidebar dot / attention bell):
      running          — Claude is working (tools running, or user just acted)
      waiting_input    — Claude finished its turn / is idle at the prompt (yellow)
      waiting_approval — Claude is BLOCKED needing the user: a permission
                         dialog, a plan approval, or an MCP elicitation form
                         (magenta + bell)

    Why every event matters:
      * Notification is Claude Code's own "needs your attention" signal — its
        matchers are the ONLY reliable way to catch permission prompts, MCP
        elicitation questionnaires, and background-agent input requests. Not
        listening to it was why a questionnaire never lit the bell.
      * UserPromptSubmit clears attention the instant the user responds — its
        absence was why the bell could stay stuck on after the user acted.
      * idle_prompt (done, waiting for next prompt) resolves to waiting_input,
        not the bell — being "done" is not the same as being "blocked".
    """
    cmd = str(_HOOK_DEST)

    def entry(state: str, matcher: str | None = None) -> dict:
        e: dict = {"hooks": [{"type": "command", "command": f"{cmd} {state}"}]}
        if matcher is not None:
            e["matcher"] = matcher
        return e

    return {
        # User acted / Claude is working → running (clears any stale attention).
        "UserPromptSubmit": [entry("running")],
        "PreToolUse": [entry("running")],
        "PostToolUse": [entry("running")],
        # Turn ended.
        "Stop": [entry("waiting_input")],
        # A permission/plan dialog is about to show → blocked on the user.
        "PermissionRequest": [entry("waiting_approval")],
        # Claude Code's attention notifications, routed by matcher. Unknown
        # matchers (e.g. agent_needs_input on older CC) are simply never
        # delivered — harmless.
        "Notification": [
            entry("waiting_approval", "permission_prompt"),
            entry("waiting_approval", "elicitation_dialog"),
            entry("waiting_approval", "agent_needs_input"),
            entry("waiting_input", "idle_prompt"),
        ],
    }


def _is_our_hook_entry(entry: dict) -> bool:
    """Check if a hook entry was installed by us."""
    hooks = entry.get("hooks", [])
    return any(_HOOK_MARKER in h.get("command", "") for h in hooks)


def install_hooks() -> None:
    """Install the hook script and register hooks in Claude Code settings.

    Idempotent — safe to call multiple times. Preserves existing settings/hooks.
    """
    # 1. Copy hook script from package to ~/.config/sw/
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_STATES_DIR.mkdir(parents=True, exist_ok=True)
    source = _get_hook_source()
    if source:
        shutil.copy2(source, _HOOK_DEST)
    elif not _HOOK_DEST.exists():
        logger.warning("Hook source script not found in package")
        return
    _HOOK_DEST.chmod(_HOOK_DEST.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # 2. Update Claude Code settings.json
    _CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)

    settings: dict = {}
    if _CLAUDE_SETTINGS.exists():
        try:
            settings = json.loads(_CLAUDE_SETTINGS.read_text())
        except (json.JSONDecodeError, OSError):
            # NEVER proceed with an empty dict here: writing it back would
            # replace the user's entire Claude Code settings with just our
            # hooks. Skip installation; state dots degrade gracefully.
            logger.warning(
                "Could not read %s — skipping hook install to avoid clobbering it",
                _CLAUDE_SETTINGS,
            )
            return

    hooks = settings.setdefault("hooks", {})
    our_hooks = _build_hooks()

    # Remove stale SW hooks from ALL events (handles renames like Notification → PermissionRequest)
    for event_name in list(hooks.keys()):
        filtered = [e for e in hooks[event_name] if not _is_our_hook_entry(e)]
        if filtered:
            hooks[event_name] = filtered
        else:
            del hooks[event_name]

    # Add our current hooks
    for event_name, our_entries in our_hooks.items():
        existing = hooks.get(event_name, [])
        existing.extend(our_entries)
        hooks[event_name] = existing

    settings["hooks"] = hooks
    _write_settings(settings)
    logger.info("Installed Claude Code hooks for state detection")


def _write_settings(settings: dict) -> None:
    """Atomic write — a crash mid-write must not corrupt Claude Code settings."""
    tmp = _CLAUDE_SETTINGS.with_suffix(".json.sw-tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n")
    tmp.rename(_CLAUDE_SETTINGS)


def uninstall_hooks() -> None:
    """Remove our hooks from Claude Code settings."""
    if not _CLAUDE_SETTINGS.exists():
        return

    try:
        settings = json.loads(_CLAUDE_SETTINGS.read_text())
    except (json.JSONDecodeError, OSError):
        return

    hooks = settings.get("hooks", {})
    changed = False

    for event_name in list(hooks.keys()):
        original = hooks[event_name]
        filtered = [e for e in original if not _is_our_hook_entry(e)]
        if len(filtered) != len(original):
            changed = True
            if filtered:
                hooks[event_name] = filtered
            else:
                del hooks[event_name]

    if changed:
        if not hooks:
            del settings["hooks"]
        _write_settings(settings)
        logger.info("Uninstalled Claude Code hooks")

    # Remove hook script
    if _HOOK_DEST.exists():
        _HOOK_DEST.unlink()
