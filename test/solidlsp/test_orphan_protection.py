"""Regression tests for issue #1490: a SIGKILLed Serena must not leave its language servers running.

``StdioLanguageServer`` starts every language server process in its own session, so that a clean
shutdown can terminate the whole process group. That code never runs if Serena itself is killed
(SIGKILL, OOM, crash), and being in its own session means no signal to Serena's process group
reaches the language server either; ``subprocess_util.ProcessGroupReaper`` closes that gap.

These tests drive a real ``StdioLanguageServer`` in a child ("fake Serena") process and check what
is still alive after that process is killed.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
import uuid

import psutil

_DRIVER_SRC = textwrap.dedent(
    """
    import sys
    import time
    from solidlsp.ls_config import LanguageServerId
    from solidlsp.ls_process import StdioLanguageServer
    from solidlsp.lsp_protocol_handler.server import ProcessLaunchInfo

    marker = sys.argv[1]
    ls = StdioLanguageServer(
        process_launch_info=ProcessLaunchInfo(
            cmd=[
                sys.executable,
                "-c",
                "import subprocess, sys, time; "
                f"subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)  # {marker}-child']); "
                f"time.sleep(300)  # {marker}",
            ],
            cwd="/tmp",
        ),
        ls_id=LanguageServerId.PYTHON,
        determine_log_level=lambda _line: 0,
    )
    ls._start()
    time.sleep(1)  # let the language server spawn its own child
    print("READY", flush=True)
    time.sleep(600)
    """
)


def _find_marked_processes(marker: str) -> list[psutil.Process]:
    found = []
    for proc in psutil.process_iter(["cmdline"]):
        try:
            cmdline = proc.info["cmdline"] or []
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if any(marker in arg for arg in cmdline):
            found.append(proc)
    return found


_THREAD_DRIVER_SRC = textwrap.dedent(
    """
    import sys
    import threading
    import time
    from solidlsp.ls_config import LanguageServerId
    from solidlsp.ls_process import StdioLanguageServer
    from solidlsp.lsp_protocol_handler.server import ProcessLaunchInfo

    marker = sys.argv[1]

    # Mirrors LanguageServerManager.from_languages's StartLSThread: the real code path spawns one
    # Thread per language server, that thread calls language_server.start() (-> ._start(), where
    # the process is actually launched), and the thread then simply returns -- terminating almost
    # immediately after Popen() returns, not on the process's main thread.
    def start_language_server():
        ls = StdioLanguageServer(
            process_launch_info=ProcessLaunchInfo(
                cmd=[sys.executable, "-c", f"import time; time.sleep(300)  # {marker}"],
                cwd="/tmp",
            ),
            ls_id=LanguageServerId.PYTHON,
            determine_log_level=lambda _line: 0,
        )
        ls.start()

    t = threading.Thread(target=start_language_server)
    t.start()
    t.join()  # the calling thread is gone from here on; the driver process itself lives on
    print("READY", flush=True)
    time.sleep(600)
    """
)


def test_language_server_process_survives_a_short_lived_calling_thread() -> None:
    """
    Starting a language server is dispatched onto a short-lived thread; the language server must
    live on after that thread has terminated, as long as Serena (the process) is alive.
    """
    marker = f"orphan-thread-test-{uuid.uuid4().hex}"
    driver = subprocess.Popen(
        [sys.executable, "-c", _THREAD_DRIVER_SRC, marker],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready_line = driver.stdout.readline()
        assert ready_line.strip() == "READY", f"driver failed to start the language server: {driver.stderr.read()}"

        time.sleep(2)
        # The driver's own argv also contains `marker` (it's passed as sys.argv[1]), so exclude
        # the driver's own pid or it would satisfy the assertion below on its own, regardless of
        # whether the actual language server process (its child) is still alive.
        survivors = [p for p in _find_marked_processes(marker) if p.pid != driver.pid]
        assert survivors, (
            "language server process died on its own even though the driver ('Serena') process "
            "is still alive -- the calling thread's exit, not a real parent death, killed it"
        )
    finally:
        for proc in _find_marked_processes(marker):
            proc.kill()
        if driver.poll() is None:
            driver.kill()


def test_language_server_process_dies_with_a_sigkilled_serena() -> None:
    marker = f"orphan-test-{uuid.uuid4().hex}"
    driver = subprocess.Popen(
        [sys.executable, "-c", _DRIVER_SRC, marker],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready_line = driver.stdout.readline()
        assert ready_line.strip() == "READY", f"driver failed to start the language server: {driver.stderr.read()}"
        started = [p for p in _find_marked_processes(marker) if p.pid != driver.pid]
        assert len(started) == 2, f"expected the language server and its child to be running, found {started}"

        driver.kill()  # SIGKILL: simulates Serena being killed without a chance to clean up
        driver.wait(timeout=5)

        deadline = time.monotonic() + 5
        survivors = _find_marked_processes(marker)
        while survivors and time.monotonic() < deadline:
            time.sleep(0.2)
            survivors = _find_marked_processes(marker)
        survivors = [p for p in survivors if p.status() != psutil.STATUS_ZOMBIE]

        assert not survivors, (
            f"language server process(es) {[p.pid for p in survivors]} survived a SIGKILLed Serena "
            "(orphaned, would run until manually killed -- issue #1490)"
        )
    finally:
        for proc in _find_marked_processes(marker):
            proc.kill()
        if driver.poll() is None:
            driver.kill()


def test_stopping_a_crashed_language_server_kills_the_processes_it_left_behind() -> None:
    """
    A language server whose main process died (e.g. typescript-language-server) may leave processes it started
    (e.g. tsserver) behind in its process group; stopping it must still terminate them.
    """
    from solidlsp.ls_config import LanguageServerId
    from solidlsp.ls_process import StdioLanguageServer
    from solidlsp.lsp_protocol_handler.server import ProcessLaunchInfo

    marker = f"crashed-ls-test-{uuid.uuid4().hex}"
    ls = StdioLanguageServer(
        process_launch_info=ProcessLaunchInfo(
            cmd=[
                sys.executable,
                "-c",
                "import subprocess, sys; "
                f"subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)  # {marker}'], "
                "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)",
            ],
            cwd="/tmp",
        ),
        ls_id=LanguageServerId.PYTHON,
        determine_log_level=lambda _line: 0,
    )
    try:
        ls.start()
        deadline = time.monotonic() + 10
        while (ls.is_running() or not _find_marked_processes(marker)) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not ls.is_running(), "the language server's main process should have exited"
        leftovers = _find_marked_processes(marker)
        assert leftovers, "the process started by the language server should still be running"

        ls.stop()

        _, alive = psutil.wait_procs(leftovers, timeout=5)
        assert not [p for p in alive if p.status() != psutil.STATUS_ZOMBIE], "processes of the crashed language server survived"
    finally:
        for proc in _find_marked_processes(marker):
            proc.kill()


_RESTARTED_WATCHER_DRIVER_SRC = textwrap.dedent(
    """
    import sys
    import time
    import psutil
    from solidlsp.ls_config import LanguageServerId
    from solidlsp.ls_process import StdioLanguageServer
    from solidlsp.lsp_protocol_handler.server import ProcessLaunchInfo

    marker = sys.argv[1]

    def start(suffix):
        ls = StdioLanguageServer(
            process_launch_info=ProcessLaunchInfo(
                cmd=[sys.executable, "-c", f"import time; time.sleep(300)  # {marker}-{suffix}"], cwd="/tmp"
            ),
            ls_id=LanguageServerId.PYTHON,
            determine_log_level=lambda _line: 0,
        )
        ls.start()
        return ls

    first = start("first")
    # kill the watcher, as a user cleaning up processes might
    for child in psutil.Process().children():
        if "groups=" in " ".join(child.cmdline()):
            child.kill()
            child.wait()
    second = start("second")
    print("READY", flush=True)
    time.sleep(600)
    """
)


def test_a_restarted_watcher_still_protects_earlier_language_servers() -> None:
    marker = f"orphan-watcher-test-{uuid.uuid4().hex}"
    driver = subprocess.Popen(
        [sys.executable, "-c", _RESTARTED_WATCHER_DRIVER_SRC, marker], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        assert driver.stdout.readline().strip() == "READY", driver.stderr.read()
        started = [p for p in _find_marked_processes(marker) if p.pid != driver.pid]
        assert len(started) == 2

        driver.kill()
        driver.wait(timeout=5)

        _, alive = psutil.wait_procs(started, timeout=10)
        assert not [p for p in alive if p.status() != psutil.STATUS_ZOMBIE], "a language server survived"
    finally:
        for proc in _find_marked_processes(marker):
            proc.kill()
        if driver.poll() is None:
            driver.kill()
