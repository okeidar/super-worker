"""Tests for PaneWatcher kqueue-based state file watcher.

Tests use real asyncio loops and real temp files.
kqueue is NOT mocked — we test the real macOS kernel-level mechanism.
"""

import asyncio
import os
import threading
import time
from pathlib import Path

import pytest

from super_worker.services.pane_watcher import PaneWatcher


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_to_file(path: Path, content: str = "x\n") -> None:
    """Append content to a file, then flush — triggers kqueue NOTE_WRITE."""
    with open(path, "a") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())


async def _wait_for(condition, *, timeout: float = 5.0, interval: float = 0.05) -> bool:
    """Poll condition() until True or timeout. Returns True if condition met."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        await asyncio.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def watcher():
    """Create a PaneWatcher, yield it, then clean up."""
    pw = PaneWatcher()
    yield pw
    pw.cleanup()


@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    """Redirect SESSION_STATES_DIR to a temp path for isolation."""
    d = tmp_path / "session-states"
    d.mkdir()
    monkeypatch.setattr("super_worker.services.pane_watcher.SESSION_STATES_DIR", d, raising=False)
    import super_worker.constants as _constants
    monkeypatch.setattr(_constants, "SESSION_STATES_DIR", d)
    return d


# ---------------------------------------------------------------------------
# 1. Thread-pool configuration
# ---------------------------------------------------------------------------

class TestThreading:
    def test_kqueue_executor_max_workers_is_one(self):
        """Each PaneWatcher's per-instance executor must be limited to 1 worker."""
        from super_worker.services.pane_watcher import PaneWatcher
        w = PaneWatcher()
        try:
            assert w._executor._max_workers == 1
        finally:
            w.cleanup()

    @pytest.mark.asyncio
    async def test_only_one_kqueue_thread_after_trigger(self, watcher, state_dir, monkeypatch):
        """After triggering the watch loop, at most one sw-kqueue thread is alive."""
        import super_worker.constants as _constants
        monkeypatch.setattr(_constants, "SESSION_STATES_DIR", state_dir)

        watcher.start_watching_state("thread-count-test", lambda n: None)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _write_to_file, state_dir / "thread-count-test")
        await asyncio.sleep(0.3)

        kq_threads = [
            t for t in threading.enumerate()
            if t.name.startswith("sw-kqueue") and t.is_alive()
        ]
        assert len(kq_threads) <= 1

    @pytest.mark.asyncio
    async def test_no_new_threads_on_additional_watches(self, watcher, state_dir, monkeypatch):
        """Adding more state watches must not spawn extra threads."""
        import super_worker.constants as _constants
        monkeypatch.setattr(_constants, "SESSION_STATES_DIR", state_dir)

        watcher.start_watching_state("thread-base", lambda n: None)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _write_to_file, state_dir / "thread-base")
        await asyncio.sleep(0.3)

        before = threading.active_count()
        for name in ("sess-a", "sess-b", "sess-c"):
            watcher.start_watching_state(name, lambda n: None)
        await asyncio.sleep(0.1)
        after = threading.active_count()

        assert after <= before + 1


# ---------------------------------------------------------------------------
# 2. State watch start/stop
# ---------------------------------------------------------------------------

class TestStateWatchStartStop:
    def test_start_watching_state_touches_file(self, watcher, state_dir, monkeypatch):
        """start_watching_state must touch() the state file."""
        import super_worker.constants as _constants
        monkeypatch.setattr(_constants, "SESSION_STATES_DIR", state_dir)

        state_file = state_dir / "test-session"
        assert not state_file.exists()
        watcher.start_watching_state("test-session", lambda n: None)
        assert state_file.exists()

    def test_stop_watching_state_removes_from_dict(self, watcher, state_dir, monkeypatch):
        """stop_watching_state must remove the session from _state_watches."""
        import super_worker.constants as _constants
        monkeypatch.setattr(_constants, "SESSION_STATES_DIR", state_dir)

        watcher.start_watching_state("remove-me", lambda n: None)
        with watcher._lock:
            assert "remove-me" in watcher._state_watches

        watcher.stop_watching_state("remove-me")
        with watcher._lock:
            assert "remove-me" not in watcher._state_watches

    def test_stop_watching_state_nonexistent_is_noop(self, watcher):
        """stop_watching_state on an unknown session must not raise."""
        watcher.stop_watching_state("never-added")


# ---------------------------------------------------------------------------
# 3. Callback fires on file write
# ---------------------------------------------------------------------------

class TestCallbackFiring:
    @pytest.mark.asyncio
    async def test_state_callback_fires_on_write(self, watcher, state_dir, monkeypatch):
        """Writing to a state file should trigger the callback via kqueue."""
        import super_worker.constants as _constants
        monkeypatch.setattr(_constants, "SESSION_STATES_DIR", state_dir)

        called_with: list[str] = []

        watcher.start_watching_state("sess-fire", lambda n: called_with.append(n))

        state_file = state_dir / "sess-fire"
        assert state_file.exists()

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _write_to_file, state_file)

        met = await _wait_for(lambda: len(called_with) > 0, timeout=5.0)
        assert met, "Callback was not called within 5 seconds after writing to state file"
        assert called_with[0] == "sess-fire"

    @pytest.mark.asyncio
    async def test_state_callback_receives_correct_session_name(self, watcher, state_dir, monkeypatch):
        """callback_arg (session_name) is forwarded correctly."""
        import super_worker.constants as _constants
        monkeypatch.setattr(_constants, "SESSION_STATES_DIR", state_dir)

        fired: list[str] = []
        watcher.start_watching_state("my-session", lambda n: fired.append(n))

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _write_to_file, state_dir / "my-session")

        met = await _wait_for(lambda: bool(fired), timeout=5.0)
        assert met, "State callback did not fire"
        assert fired[0] == "my-session"


# ---------------------------------------------------------------------------
# 4. Multiple watches simultaneously — only correct callback fires
# ---------------------------------------------------------------------------

class TestMultipleWatches:
    @pytest.mark.asyncio
    async def test_only_target_callback_fires(self, watcher, state_dir, monkeypatch):
        """Writing to sess-B's file fires sess-B callback, not sess-A or sess-C."""
        import super_worker.constants as _constants
        monkeypatch.setattr(_constants, "SESSION_STATES_DIR", state_dir)

        fired: dict[str, list[str]] = {"a": [], "b": [], "c": []}

        watcher.start_watching_state("sess-a", lambda n: fired["a"].append(n))
        watcher.start_watching_state("sess-b", lambda n: fired["b"].append(n))
        watcher.start_watching_state("sess-c", lambda n: fired["c"].append(n))

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _write_to_file, state_dir / "sess-b")

        met = await _wait_for(lambda: bool(fired["b"]), timeout=5.0)
        assert met, "sess-b callback did not fire"
        await asyncio.sleep(0.3)
        assert fired["a"] == [], "sess-a should not have fired"
        assert fired["c"] == [], "sess-c should not have fired"

    @pytest.mark.asyncio
    async def test_all_callbacks_can_fire_independently(self, watcher, state_dir, monkeypatch):
        """Each of three sessions fires its own callback when its file is written."""
        import super_worker.constants as _constants
        monkeypatch.setattr(_constants, "SESSION_STATES_DIR", state_dir)

        fired: dict[str, list[str]] = {"x": [], "y": [], "z": []}

        for name in ("x", "y", "z"):
            watcher.start_watching_state(name, lambda n, d=fired: d[n].append(n))

        loop = asyncio.get_running_loop()
        for name in ("x", "y", "z"):
            await loop.run_in_executor(None, _write_to_file, state_dir / name)
            await asyncio.sleep(0.05)

        for name in ("x", "y", "z"):
            met = await _wait_for(lambda n=name: bool(fired[n]), timeout=5.0)
            assert met, f"Callback for {name} did not fire"


# ---------------------------------------------------------------------------
# 5. Replace watch
# ---------------------------------------------------------------------------

class TestReplaceWatch:
    @pytest.mark.asyncio
    async def test_second_start_watching_state_replaces_first(self, watcher, state_dir, monkeypatch):
        """Calling start_watching_state twice for same session replaces the watch."""
        import super_worker.constants as _constants
        monkeypatch.setattr(_constants, "SESSION_STATES_DIR", state_dir)

        first_fired: list[str] = []
        second_fired: list[str] = []

        watcher.start_watching_state("dup-sess", lambda n: first_fired.append(n))
        watcher.start_watching_state("dup-sess", lambda n: second_fired.append(n))

        with watcher._lock:
            assert list(watcher._state_watches.keys()).count("dup-sess") == 1

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _write_to_file, state_dir / "dup-sess")

        met = await _wait_for(lambda: bool(second_fired), timeout=5.0)
        assert met, "Replacement callback did not fire"
        await asyncio.sleep(0.2)
        assert first_fired == [], "Old callback fired after replacement"


# ---------------------------------------------------------------------------
# 6. cleanup()
# ---------------------------------------------------------------------------

class TestCleanup:
    @pytest.mark.asyncio
    async def test_cleanup_stops_running_and_cancels_task(self, watcher, state_dir, monkeypatch):
        """cleanup() sets _running=False and cancels the asyncio task."""
        import super_worker.constants as _constants
        monkeypatch.setattr(_constants, "SESSION_STATES_DIR", state_dir)

        watcher.start_watching_state("cleanup-test", lambda n: None)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _write_to_file, state_dir / "cleanup-test")
        await asyncio.sleep(0.2)

        task = watcher._task
        assert task is not None
        assert not task.done()

        watcher.cleanup()

        assert watcher._running is False
        await asyncio.sleep(0.6)
        assert task.done()

    def test_cleanup_clears_all_watches(self, watcher, state_dir, monkeypatch):
        """cleanup() removes all registered watches."""
        import super_worker.constants as _constants
        monkeypatch.setattr(_constants, "SESSION_STATES_DIR", state_dir)

        watcher.start_watching_state("c1", lambda n: None)
        watcher.start_watching_state("c2", lambda n: None)

        watcher.cleanup()

        with watcher._lock:
            assert watcher._state_watches == {}
            assert watcher._fd_to_watch == {}

    def test_double_cleanup_does_not_raise(self):
        """Calling cleanup() twice should not raise any exception."""
        pw = PaneWatcher()
        pw.cleanup()
        pw.cleanup()


# ---------------------------------------------------------------------------
# 7. stop_watching_state removes the watch
# ---------------------------------------------------------------------------

class TestStopWatchingState:
    @pytest.mark.asyncio
    async def test_callback_does_not_fire_after_stop(self, watcher, state_dir, monkeypatch):
        """After stop_watching_state, writes to the file do not trigger callback."""
        import super_worker.constants as _constants
        monkeypatch.setattr(_constants, "SESSION_STATES_DIR", state_dir)

        fired: list[str] = []

        watcher.start_watching_state("stopped-sess", lambda n: fired.append(n))
        watcher.stop_watching_state("stopped-sess")

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _write_to_file, state_dir / "stopped-sess")

        await asyncio.sleep(0.5)
        assert fired == [], "Callback fired after stop_watching_state"


# ---------------------------------------------------------------------------
# 8. Internal API
# ---------------------------------------------------------------------------

class TestInternalAPIContracts:
    def test_no_pipe_watches_attribute(self, watcher):
        """Pipe-pane watching removed — _pipe_watches attribute must not exist."""
        assert not hasattr(watcher, "_pipe_watches"), (
            "_pipe_watches found — pipe-pane watching was removed"
        )

    def test_state_watches_attribute_exists(self, watcher):
        assert hasattr(watcher, "_state_watches")

    def test_fd_to_watch_attribute_exists(self, watcher):
        assert hasattr(watcher, "_fd_to_watch")

    def test_kqueue_attribute_is_kqueue(self, watcher):
        import select
        assert isinstance(watcher._kq, select.kqueue)


# ---------------------------------------------------------------------------
# 9. TerminalPane.resume_watching uses fallback timer, no pipe-pane API
# ---------------------------------------------------------------------------

class TestTerminalPaneResume:
    def test_resume_watching_no_pipe_pane_calls(self):
        """resume_watching must not call start_watching (pipe-pane removed)."""
        import ast
        src = Path("/Users/omerkeidar/Projects/super-worker/super_worker/widgets/terminal_pane.py").read_text()
        tree = ast.parse(src)

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == "resume_watching":
                    func_src = ast.get_source_segment(src, node)
                    assert func_src is not None
                    assert "start_watching(" not in func_src, (
                        "resume_watching still calls start_watching (pipe-pane)"
                    )
                    assert "stop_watching(" not in func_src, (
                        "resume_watching still calls stop_watching (pipe-pane)"
                    )
                    return

        pytest.fail("resume_watching method not found in terminal_pane.py")
