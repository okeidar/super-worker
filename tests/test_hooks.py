import json
from pathlib import Path

from super_worker.services.hooks import install_hooks, uninstall_hooks


def _setup_hooks_env(tmp_path, monkeypatch):
    """Common setup: point hooks module at tmp_path for isolation."""
    hook_dest = tmp_path / "sw-hook.sh"
    claude_settings = tmp_path / ".claude" / "settings.json"

    # Mock _get_hook_source to return a fake script
    hook_source = tmp_path / "source" / "sw-hook.sh"
    hook_source.parent.mkdir(parents=True)
    hook_source.write_text("#!/bin/bash\necho test")
    monkeypatch.setattr("super_worker.services.hooks._get_hook_source", lambda: hook_source)

    monkeypatch.setattr("super_worker.services.hooks._HOOK_DEST", hook_dest)
    monkeypatch.setattr("super_worker.services.hooks._CLAUDE_SETTINGS", claude_settings)
    monkeypatch.setattr("super_worker.services.hooks.STATE_DIR", tmp_path)
    return hook_dest, claude_settings


def test_install_hooks_creates_script_and_settings(tmp_path, monkeypatch):
    """install_hooks() copies the script and adds hooks to settings.json."""
    hook_dest, claude_settings = _setup_hooks_env(tmp_path, monkeypatch)

    install_hooks()

    assert hook_dest.exists()
    assert claude_settings.exists()

    settings = json.loads(claude_settings.read_text())
    assert "hooks" in settings
    assert "Stop" in settings["hooks"]
    assert "PermissionRequest" in settings["hooks"]
    assert "PreToolUse" in settings["hooks"]

    # Verify hook commands reference our script
    stop_hooks = settings["hooks"]["Stop"]
    assert len(stop_hooks) == 1
    assert "sw-hook.sh" in stop_hooks[0]["hooks"][0]["command"]
    assert "waiting_input" in stop_hooks[0]["hooks"][0]["command"]


def test_install_hooks_idempotent(tmp_path, monkeypatch):
    """Running install_hooks() twice doesn't duplicate hook entries."""
    _setup_hooks_env(tmp_path, monkeypatch)

    install_hooks()
    install_hooks()

    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert len(settings["hooks"]["Stop"]) == 1
    assert len(settings["hooks"]["PermissionRequest"]) == 1
    assert len(settings["hooks"]["PreToolUse"]) == 1


def test_install_hooks_preserves_existing(tmp_path, monkeypatch):
    """install_hooks() preserves existing settings and hooks."""
    hook_dest, claude_settings = _setup_hooks_env(tmp_path, monkeypatch)
    claude_settings.parent.mkdir(parents=True, exist_ok=True)

    # Pre-existing settings with a custom hook
    existing = {
        "apiKey": "sk-test",
        "hooks": {
            "Stop": [
                {"hooks": [{"type": "command", "command": "echo custom"}]}
            ]
        }
    }
    claude_settings.write_text(json.dumps(existing))

    install_hooks()

    settings = json.loads(claude_settings.read_text())
    # Preserved existing key
    assert settings["apiKey"] == "sk-test"
    # Preserved existing custom hook + added ours
    stop_hooks = settings["hooks"]["Stop"]
    assert len(stop_hooks) == 2
    assert stop_hooks[0]["hooks"][0]["command"] == "echo custom"


def test_uninstall_hooks_removes_our_entries(tmp_path, monkeypatch):
    """uninstall_hooks() removes SW hooks but preserves others."""
    hook_dest = tmp_path / "sw-hook.sh"
    hook_dest.write_text("#!/bin/bash")
    claude_settings = tmp_path / ".claude" / "settings.json"
    claude_settings.parent.mkdir(parents=True)

    settings = {
        "hooks": {
            "Stop": [
                {"hooks": [{"type": "command", "command": "echo custom"}]},
                {"hooks": [{"type": "command", "command": f"{hook_dest} waiting_input"}]},
            ],
            "PreToolUse": [
                {"hooks": [{"type": "command", "command": f"{hook_dest} running"}]},
            ],
        }
    }
    claude_settings.write_text(json.dumps(settings))

    monkeypatch.setattr("super_worker.services.hooks._HOOK_DEST", hook_dest)
    monkeypatch.setattr("super_worker.services.hooks._CLAUDE_SETTINGS", claude_settings)

    uninstall_hooks()

    result = json.loads(claude_settings.read_text())
    # Custom hook preserved
    assert len(result["hooks"]["Stop"]) == 1
    assert result["hooks"]["Stop"][0]["hooks"][0]["command"] == "echo custom"
    # PreToolUse removed entirely (was only our hook)
    assert "PreToolUse" not in result["hooks"]
    # Hook script removed
    assert not hook_dest.exists()


def _sw_cmd(entry):
    return entry["hooks"][0]["command"]


def test_notification_matchers_route_to_states(tmp_path, monkeypatch):
    """Notification is routed by matcher: approval/elicitation/agent-input → bell,
    idle → waiting_input. This is what makes questionnaires light up the bell."""
    _, claude_settings = _setup_hooks_env(tmp_path, monkeypatch)
    install_hooks()
    hooks = json.loads(claude_settings.read_text())["hooks"]

    notif = hooks["Notification"]
    by_matcher = {e.get("matcher"): _sw_cmd(e) for e in notif}
    assert "waiting_approval" in by_matcher["permission_prompt"]
    assert "waiting_approval" in by_matcher["elicitation_dialog"]
    assert "waiting_approval" in by_matcher["agent_needs_input"]
    assert "waiting_input" in by_matcher["idle_prompt"]


def test_user_prompt_submit_clears_attention(tmp_path, monkeypatch):
    """UserPromptSubmit → running so the bell clears the moment the user acts."""
    _, claude_settings = _setup_hooks_env(tmp_path, monkeypatch)
    install_hooks()
    hooks = json.loads(claude_settings.read_text())["hooks"]
    assert "UserPromptSubmit" in hooks
    assert "running" in _sw_cmd(hooks["UserPromptSubmit"][0])


def test_install_idempotent_with_notification_matchers(tmp_path, monkeypatch):
    """Re-installing doesn't duplicate the multi-entry Notification hooks."""
    _, claude_settings = _setup_hooks_env(tmp_path, monkeypatch)
    install_hooks()
    install_hooks()
    hooks = json.loads(claude_settings.read_text())["hooks"]
    assert len(hooks["Notification"]) == 4
    assert len(hooks["UserPromptSubmit"]) == 1


def test_uninstall_removes_notification_and_userprompt(tmp_path, monkeypatch):
    """uninstall removes the new events too, preserving unrelated user hooks."""
    _, claude_settings = _setup_hooks_env(tmp_path, monkeypatch)
    claude_settings.parent.mkdir(parents=True, exist_ok=True)
    claude_settings.write_text(json.dumps({
        "hooks": {"Notification": [{"matcher": "idle_prompt",
                                    "hooks": [{"type": "command", "command": "echo mine"}]}]},
    }))
    install_hooks()
    uninstall_hooks()
    hooks = json.loads(claude_settings.read_text()).get("hooks", {})
    # our entries gone; the user's custom Notification hook preserved
    remaining = hooks.get("Notification", [])
    assert all("sw-hook.sh" not in _sw_cmd(e) for e in remaining)
    assert any(_sw_cmd(e) == "echo mine" for e in remaining)
    assert "UserPromptSubmit" not in hooks
