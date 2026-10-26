"""
Process lifecycle handling for long-running Serena processes on macOS.
"""

import logging
import os
import select
import signal
import threading
from collections.abc import Callable
from types import FrameType

log = logging.getLogger(__name__)


class ProcessLifecycle:
    """
    Makes sure that the process shuts down cleanly (running the registered shutdown actions) when it is asked
    to terminate (SIGTERM, SIGHUP, SIGINT) or when its parent process exits, which is how an MCP client going
    away without closing our stdin manifests itself (e.g. when it kills only the launcher, such as ``uvx``).

    A shutdown that does not complete within :attr:`SHUTDOWN_GRACE_PERIOD` seconds is cut short by exiting
    immediately; language server processes are reaped regardless (see ``solidlsp.util.subprocess_util``).
    """

    SHUTDOWN_GRACE_PERIOD = 10.0

    def __init__(self) -> None:
        self._shutdown_actions: list[Callable[[], None]] = []
        self._shutdown_lock = threading.Lock()
        self._shutting_down = False

    def add_shutdown_action(self, action: Callable[[], None]) -> None:
        """
        :param action: an action to run (once) when the process shuts down; actions run in the order they were added
        """
        self._shutdown_actions.append(action)

    def install(self, watch_parent: bool = True) -> None:
        """
        Installs the signal handlers (must be called from the main thread) and, optionally, the parent watch.

        :param watch_parent: whether to shut down when the parent process exits
        """
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(sig, self._handle_signal)
        if watch_parent:
            threading.Thread(target=self._watch_parent, args=(os.getppid(),), name="ParentWatch", daemon=True).start()

    def _handle_signal(self, signum: int, _frame: FrameType | None) -> None:
        self.shutdown(f"received {signal.Signals(signum).name}")

    def _watch_parent(self, parent_pid: int) -> None:
        if parent_pid == 1:
            self.shutdown("the parent process exited before the watch was installed")
            return
        kq = select.kqueue()
        try:
            event = select.kevent(
                parent_pid,
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                fflags=select.KQ_NOTE_EXIT,
            )
            try:
                kq.control([event], 0)
            except ProcessLookupError:
                self.shutdown(f"the parent process {parent_pid} exited before the watch was installed")
                return
            kq.control(None, 1)  # blocks until the parent exits
        finally:
            kq.close()
        self.shutdown(f"the parent process {parent_pid} exited")

    def shutdown(self, reason: str) -> None:
        """
        Runs the shutdown actions (only once, even if called repeatedly or concurrently) and exits the process.

        :param reason: the reason for the shutdown, for logging
        """
        with self._shutdown_lock:
            if self._shutting_down:
                return
            self._shutting_down = True
        log.info(f"Shutting down: {reason}")

        # make sure the process exits even if a shutdown action hangs
        watchdog = threading.Timer(self.SHUTDOWN_GRACE_PERIOD, lambda: os._exit(1))
        watchdog.daemon = True
        watchdog.start()

        for action in self._shutdown_actions:
            try:
                action()
            except Exception as e:
                log.error(f"Error during shutdown: {e}", exc_info=e)
        logging.shutdown()
        os._exit(0)
