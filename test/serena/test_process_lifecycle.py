"""
End-to-end tests of the guarantee that a Serena MCP server never leaves language servers behind: when it is
asked to terminate, and when the process that launched it goes away without telling it.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import psutil
import pytest

from solidlsp.ls_config import LanguageServerId
from test.conftest import get_repo_path

pytestmark = pytest.mark.python


def _rpc(proc: subprocess.Popen, message: dict) -> None:
    assert proc.stdin is not None
    proc.stdin.write((json.dumps(message) + "\n").encode())
    proc.stdin.flush()


def _read_response(proc: subprocess.Popen, request_id: int, timeout: float = 120.0) -> dict:
    assert proc.stdout is not None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            raise AssertionError("the MCP server closed its stdout")
        message = json.loads(line)
        if message.get("id") == request_id:
            return message
    raise AssertionError(f"no response to request {request_id}")


def _descendants(pid: int) -> list[psutil.Process]:
    try:
        return psutil.Process(pid).children(recursive=True)
    except psutil.NoSuchProcess:
        return []


def _wait_until_gone(processes: list[psutil.Process], timeout: float = 15.0) -> list[psutil.Process]:
    deadline = time.monotonic() + timeout
    alive = processes
    while alive and time.monotonic() < deadline:
        time.sleep(0.2)
        alive = [p for p in alive if p.is_running() and p.status() != psutil.STATUS_ZOMBIE]
    return alive


@pytest.fixture
def serena_env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    project_root = tmp_path / "project"
    shutil.copytree(get_repo_path(LanguageServerId.PYTHON), project_root)
    env = dict(os.environ, SERENA_HOME=str(tmp_path / "serena_home"))
    return env, project_root


def _start_serena_with_running_language_server(cmd_prefix: list[str], env: dict[str, str], project_root: Path) -> subprocess.Popen:
    """
    Starts Serena (possibly via a wrapper process, given by ``cmd_prefix``) on the project and makes a tool call that
    starts the language server.
    """
    serena_cmd = [
        str(Path(sys.executable).parent / "serena"),
        "start-mcp-server",
        "--context",
        "claude-code",
        "--project",
        str(project_root),
    ]
    proc = subprocess.Popen(
        cmd_prefix + serena_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, cwd=project_root
    )
    _rpc(
        proc,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
        },
    )
    _read_response(proc, 1)
    _rpc(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    _rpc(
        proc,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "get_symbols_overview", "arguments": {"relative_path": "test_repo/models.py"}},
        },
    )
    response = _read_response(proc, 2)
    assert "User" in json.dumps(response), f"the tool call did not reach the language server: {response}"
    return proc


def _find_serena_pid(proc: subprocess.Popen) -> int:
    for p in [psutil.Process(proc.pid), *_descendants(proc.pid)]:
        if "start-mcp-server" in " ".join(p.cmdline()):
            return p.pid
    raise AssertionError("Serena process not found")


def test_terminated_serena_stops_its_language_servers(serena_env: tuple[dict[str, str], Path]) -> None:
    env, project_root = serena_env
    proc = _start_serena_with_running_language_server([], env, project_root)
    try:
        language_server_processes = _descendants(proc.pid)
        assert language_server_processes, "expected a running language server"

        proc.send_signal(signal.SIGTERM)

        proc.wait(timeout=15)
        assert not _wait_until_gone(language_server_processes)
    finally:
        if proc.poll() is None:
            proc.kill()


def test_serena_exits_when_its_parent_dies(serena_env: tuple[dict[str, str], Path]) -> None:
    """
    An MCP client that kills only the launcher (e.g. ``uvx``) never closes Serena's stdin; Serena must notice
    that its parent is gone and shut down, taking its language servers with it.
    """
    env, project_root = serena_env
    # the wrapper stands in for a launcher like uvx: it runs Serena as a child, passing its stdio through
    wrapper = [
        sys.executable,
        "-c",
        textwrap.dedent(
            """
            import subprocess, sys
            subprocess.run(sys.argv[1:])
            """
        ),
    ]
    proc = _start_serena_with_running_language_server(wrapper, env, project_root)
    try:
        serena = psutil.Process(_find_serena_pid(proc))
        language_server_processes = _descendants(serena.pid)
        assert language_server_processes, "expected a running language server"

        proc.kill()  # the launcher dies; Serena's stdin stays open (our pipe is still held by this test)
        proc.wait(timeout=5)

        assert not _wait_until_gone([serena]), "Serena kept running after its parent died"
        assert not _wait_until_gone(language_server_processes), "language servers survived"
    finally:
        for p in _descendants(proc.pid):
            p.kill()
        if proc.poll() is None:
            proc.kill()
