"""Tests for language server process-group cleanup.

Every language server process is started in its own session, which makes it the leader of its
own process group (PGID equal to its PID). Cleanup signals the whole group via ``os.killpg``,
which needs no enumeration of the system process table (that can be denied in a sandboxed macOS
environment, see issue #1818). No language markers: these exercise ``subprocess_util`` directly
with plain Python helper processes and run in catch-all.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from solidlsp.util.subprocess_util import _signal_process_group, terminate_process_group_with_kill_fallback


def _group_is_gone(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return False
    except ProcessLookupError:
        return True


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _spawn_ready(src: str) -> subprocess.Popen:
    """Starts ``src`` in its own session and waits for it to print READY (deterministic sync instead of a blind sleep)."""
    proc = subprocess.Popen([sys.executable, "-c", src], start_new_session=True, stdout=subprocess.PIPE, text=True)
    ready_line = proc.stdout.readline()
    assert ready_line.strip() == "READY", f"helper process failed to start: {ready_line!r}"
    return proc


class TestSignalProcessGroup:
    def test_nonexistent_group_is_treated_as_already_clean(self) -> None:
        bogus_pgid = 2**30  # not a real PGID; must be handled like an already-gone group
        _signal_process_group(bogus_pgid, terminate=True)  # must not raise

    def test_permission_error_is_caught_and_logged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def raise_eperm(pgid: int, sig: int) -> None:
            raise PermissionError("simulated sandbox denial")

        monkeypatch.setattr(os, "killpg", raise_eperm)
        _signal_process_group(12345, terminate=True)  # must not raise


class TestTerminateProcessGroupWithKillFallback:
    def test_terminates_child_and_grandchild_via_group_id(self) -> None:
        """A focused POSIX cleanup test: start a child and grandchild in one new session,
        invoke the cleanup utility by PGID only, and verify both are gone, without ever
        calling psutil.
        """
        src = textwrap.dedent(
            """
            import subprocess, sys, time
            subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
            print("READY", flush=True)
            time.sleep(300)
            """
        )
        proc = _spawn_ready(src)
        pgid = proc.pid
        try:
            assert not _group_is_gone(pgid), "process group should be alive before cleanup"
            terminate_process_group_with_kill_fallback(proc, pgid, terminate_timeout=5.0)
            assert _wait_until(lambda: _group_is_gone(pgid)), f"process group {pgid} survived cleanup"
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2.0)

    def test_falls_back_to_kill_when_group_ignores_sigterm(self) -> None:
        src = textwrap.dedent(
            """
            import signal, time
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            print("READY", flush=True)
            time.sleep(300)
            """
        )
        proc = _spawn_ready(src)
        pgid = proc.pid
        try:
            terminate_process_group_with_kill_fallback(proc, pgid, terminate_timeout=1.0)
            assert _wait_until(lambda: _group_is_gone(pgid)), f"process group {pgid} survived SIGKILL fallback"
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2.0)

    def test_already_exited_group_does_not_raise(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        pgid = proc.pid
        proc.wait(timeout=5.0)
        assert _wait_until(lambda: _group_is_gone(pgid)), "group should be released once the leader is reaped"
        terminate_process_group_with_kill_fallback(proc, pgid, terminate_timeout=1.0)

    def test_kills_children_that_outlive_a_terminated_leader(self) -> None:
        """A leader that exits on SIGTERM must not leave behind a child that ignores SIGTERM."""
        src = textwrap.dedent(
            """
            import subprocess, sys, time
            child_src = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('UP', flush=True); time.sleep(300)"
            child = subprocess.Popen([sys.executable, "-c", child_src], stdout=subprocess.PIPE, text=True)
            child.stdout.readline()
            print(child.pid, flush=True)
            print("READY", flush=True)
            time.sleep(300)
            """
        )
        proc = subprocess.Popen([sys.executable, "-c", src], start_new_session=True, stdout=subprocess.PIPE, text=True)
        child_pid = int(proc.stdout.readline().strip())
        assert proc.stdout.readline().strip() == "READY"
        pgid = proc.pid
        try:
            terminate_process_group_with_kill_fallback(proc, pgid, terminate_timeout=5.0)
            assert _wait_until(lambda: not _process_alive(child_pid)), "child ignoring SIGTERM outlived its terminated leader"
        finally:
            for pid in (child_pid, proc.pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if proc.poll() is None:
                proc.wait(timeout=2.0)
