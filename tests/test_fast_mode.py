"""Fast-mode tests — focus on shell-injection safety and id-based resolution.

These use a REAL tmux server on an isolated socket (never the default one),
so the security property (menu bindings never route names through a shell) is
verified end-to-end, not just asserted about strings.
"""

import shutil
import time
from pathlib import Path

import libtmux
import pytest

import super_worker.services.tmux as tmux_mod
from super_worker.services.fast_ui import (
    _configure_host_session,
    resolve_target,
    resolve_window_ref,
    worktree_name_from_window,
)

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")


class _Cfg:
    repo_root = Path("/tmp")


@pytest.fixture
def tmux_server(monkeypatch):
    """An isolated tmux server; wired in as the module server and torn down."""
    server = libtmux.Server(socket_name="sw-test-fastmode")
    monkeypatch.setattr(tmux_mod, "_server", server)
    yield server
    try:
        server.kill()
    except Exception:
        pass


def test_menu_bindings_use_ids_not_names(tmux_server):
    """The generated keybindings must never interpolate #{window_name}.

    Window names embed git branch names (arbitrary text incl. quotes/$/`),
    so interpolating them into shell commands is an injection vector.
    """
    sess = tmux_server.new_session(session_name="sw-fast-t", start_directory="/tmp")
    _configure_host_session(sess, _Cfg())
    out = "\n".join(tmux_server.cmd("list-keys", "-T", "prefix").stdout)
    sw_lines = "\n".join(l for l in out.splitlines() if "fast-" in l or "Super Worker" in l)

    assert sw_lines, "sw bindings should be present"
    assert "#{window_name}" not in sw_lines, "bindings must not interpolate window names"
    assert "#{window_id}" in sw_lines, "bindings should pass window ids"
    assert "#{pane_id}" in sw_lines
    assert "%%" not in sw_lines, "rename must not use tmux command-prompt %% substitution"


def test_malicious_branch_name_does_not_execute(tmux_server, tmp_path):
    """A window name crafted to break out of a shell string stays inert."""
    marker = tmp_path / "pwned"
    sess = tmux_server.new_session(session_name="sw-fast-evil", start_directory="/tmp")
    evil = f"x'$(touch {marker})' (evil ↑1↓2)"
    sess.windows[0].rename_window(evil)
    wid = sess.windows[0].window_id

    # Resolution keeps the name as inert data
    name, _ = resolve_window_ref(wid)
    assert name == "x'$(touch " + str(marker) + ")'"

    _configure_host_session(sess, _Cfg())
    time.sleep(0.3)
    assert not marker.exists(), "branch-name payload must never execute"


def test_resolve_window_ref_accepts_legacy_name():
    """A raw window name (stale binding) still resolves to a worktree name."""
    name, ctx = resolve_window_ref("myfeature (sw-myfeature ↑0↓0)*")
    assert name == "myfeature"
    assert ctx is None  # names carry no cwd context


def test_worktree_name_strips_attention_and_branch():
    assert worktree_name_from_window("! feat (sw-feat ↑1↓2)*") == "feat"


def test_resolve_target_returns_context(tmux_server):
    sess = tmux_server.new_session(session_name="sw-fast-ctx", start_directory="/tmp")
    info = resolve_target(sess.windows[0].active_pane.pane_id)
    assert info is not None
    assert info["session_name"] == "sw-fast-ctx"
    assert info["pane_id"].startswith("%")
    # macOS /tmp is a symlink to /private/tmp — either is acceptable
    assert info["pane_path"] in ("/tmp", "/private/tmp")
