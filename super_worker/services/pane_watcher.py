"""Watch session state files via kqueue for efficient bell-icon updates."""

import asyncio
import concurrent.futures
import logging
import os
import select
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass
class _Watch:
    """A single watched fd with its dispatch callback."""
    fd: int
    callback: Callable
    callback_arg: str  # session_name for state watches


class PaneWatcher:
    """Watches session state files via kqueue for state change notifications.

    Each instance owns a dedicated 1-thread executor for its blocking kqueue
    call. A shared global executor caused N PaneWatchers to serialize on a
    single thread (each blocking for up to 0.5 s), making state-event latency
    grow as N × 0.5 s with multiple open projects.
    """

    def __init__(self) -> None:
        self._kq = select.kqueue()
        self._lock = threading.Lock()
        self._fd_to_watch: dict[int, _Watch] = {}
        self._state_watches: dict[str, _Watch] = {}
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="sw-kqueue"
        )
        self._running = True
        self._task: asyncio.Task | None = None

    # ── Public API ────────────────────────────────────────────────────────────

    def start_watching_state(self, session_name: str, callback: Callable) -> None:
        """Watch a session's state file. Calls callback(session_name) on change."""
        if session_name in self._state_watches:
            self.stop_watching_state(session_name)

        from super_worker.constants import SESSION_STATES_DIR
        state_file = SESSION_STATES_DIR / session_name
        SESSION_STATES_DIR.mkdir(parents=True, exist_ok=True)
        state_file.touch()

        try:
            fd = os.open(str(state_file), os.O_RDONLY)
        except OSError:
            logger.debug("Failed to open state file: %s", state_file)
            return

        watch = _Watch(fd=fd, callback=callback, callback_arg=session_name)
        self._register(watch)
        with self._lock:
            self._state_watches[session_name] = watch

    def stop_watching_state(self, session_name: str) -> None:
        """Stop watching a session's state file."""
        with self._lock:
            watch = self._state_watches.pop(session_name, None)
        if watch is None:
            return
        self._unregister(watch)

    def cleanup(self) -> None:
        """Stop all watches and shut down."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            self._task = None
        for name in list(self._state_watches):
            self.stop_watching_state(name)
        try:
            self._kq.close()
        except Exception:
            pass
        self._executor.shutdown(wait=False)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _register(self, watch: _Watch) -> None:
        """Add fd to the kqueue and start the watcher task if needed."""
        kevent = select.kevent(
            watch.fd,
            filter=select.KQ_FILTER_VNODE,
            flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
            fflags=select.KQ_NOTE_WRITE,
        )
        try:
            self._kq.control([kevent], 0, 0)
        except Exception:
            try:
                os.close(watch.fd)
            except OSError:
                pass
            return
        with self._lock:
            self._fd_to_watch[watch.fd] = watch

        # Ensure the watcher asyncio task is running
        try:
            loop = asyncio.get_running_loop()
            if self._task is None or self._task.done():
                self._task = loop.create_task(self._watch_loop())
        except RuntimeError:
            pass

    def _unregister(self, watch: _Watch) -> None:
        """Remove fd from the kqueue and close it."""
        with self._lock:
            self._fd_to_watch.pop(watch.fd, None)
        kevent = select.kevent(
            watch.fd,
            filter=select.KQ_FILTER_VNODE,
            flags=select.KQ_EV_DELETE,
        )
        try:
            self._kq.control([kevent], 0, 0)
        except Exception:
            pass
        try:
            os.close(watch.fd)
        except OSError:
            pass

    async def _watch_loop(self) -> None:
        """Single asyncio task: blocks on kqueue in dedicated thread, fires callbacks."""
        loop = asyncio.get_running_loop()
        try:
            while self._running:
                try:
                    events = await loop.run_in_executor(
                        self._executor, self._kq.control, None, 32, 0.5
                    )
                except Exception:
                    if self._running:
                        logger.debug("kqueue error in watch loop", exc_info=True)
                    break

                for event in events:
                    fd = event.ident
                    with self._lock:
                        watch = self._fd_to_watch.get(fd)
                    if watch is None:
                        continue
                    try:
                        watch.callback(watch.callback_arg)
                    except Exception:
                        logger.debug("kqueue callback error", exc_info=True)
        except asyncio.CancelledError:
            pass
