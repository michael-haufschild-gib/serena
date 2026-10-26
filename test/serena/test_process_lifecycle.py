"""
End-to-end tests of the process lifecycle of Serena MCP sessions: a session served in-process never leaves
language servers behind, and sessions on the same project share one server (and thus one set of language servers),
which exits once it has been idle.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator, Sequence
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


def _shared_servers(project_root: Path) -> list[psutil.Process]:
    result = []
    for p in psutil.process_iter(["cmdline"]):
        cmdline = " ".join(p.info["cmdline"] or [])
        if "serena.project_daemon" in cmdline and str(project_root) in cmdline:
            result.append(p)
    return result


@pytest.fixture
def serena_env(tmp_path: Path) -> Iterator[tuple[dict[str, str], Path]]:
    project_root = tmp_path / "project"
    shutil.copytree(get_repo_path(LanguageServerId.PYTHON), project_root)
    serena_home = tmp_path / "serena_home"
    serena_home.mkdir()
    (serena_home / "serena_config.yml").write_text("projects: []\nls_idle_timeout: 2\n")
    env = dict(os.environ, SERENA_HOME=str(serena_home))
    yield env, project_root
    for server in _shared_servers(project_root):
        for p in [*server.children(recursive=True), server]:
            try:
                p.kill()
            except psutil.NoSuchProcess:
                pass


def _start_session(env: dict[str, str], project_root: Path, shared_server: bool, cmd_prefix: Sequence[str] = ()) -> subprocess.Popen:
    """
    Starts an MCP session (possibly via a wrapper process, given by ``cmd_prefix``) on the project and makes a tool call
    that requires the language server.
    """
    serena_cmd = [
        str(Path(sys.executable).parent / "serena"),
        "start-mcp-server",
        "--context",
        "claude-code",
        "--project",
        str(project_root),
        "--shared-server" if shared_server else "--no-shared-server",
    ]
    proc = subprocess.Popen(
        [*cmd_prefix, *serena_cmd], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, cwd=project_root
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
    _call_symbols_overview(proc, request_id=2)
    return proc


def _call_symbols_overview(proc: subprocess.Popen, request_id: int) -> None:
    _rpc(
        proc,
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": "get_symbols_overview", "arguments": {"relative_path": "test_repo/models.py"}},
        },
    )
    response = _read_response(proc, request_id)
    assert "User" in json.dumps(response), f"the tool call did not reach the language server: {response}"


def _find_serena_pid(proc: subprocess.Popen) -> int:
    for p in [psutil.Process(proc.pid), *_descendants(proc.pid)]:
        if "start-mcp-server" in " ".join(p.cmdline()):
            return p.pid
    raise AssertionError("Serena process not found")


def test_terminated_in_process_serena_stops_its_language_servers(serena_env: tuple[dict[str, str], Path]) -> None:
    env, project_root = serena_env
    proc = _start_session(env, project_root, shared_server=False)
    try:
        language_server_processes = _descendants(proc.pid)
        assert language_server_processes, "expected a running language server"

        proc.send_signal(signal.SIGTERM)

        proc.wait(timeout=15)
        assert not _wait_until_gone(language_server_processes)
    finally:
        if proc.poll() is None:
            proc.kill()


def test_in_process_serena_exits_when_its_parent_dies(serena_env: tuple[dict[str, str], Path]) -> None:
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
    proc = _start_session(env, project_root, shared_server=False, cmd_prefix=wrapper)
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


def _language_server_processes(server: psutil.Process) -> list[psutil.Process]:
    return [p for p in server.children(recursive=True) if "pyright" in " ".join(p.cmdline())]


def test_sessions_on_a_project_share_one_language_server(serena_env: tuple[dict[str, str], Path]) -> None:
    env, project_root = serena_env
    first = _start_session(env, project_root, shared_server=True)
    second = _start_session(env, project_root, shared_server=True)
    try:
        servers = _shared_servers(project_root)
        assert len(servers) == 1, f"expected one shared server, found {servers}"
        assert len(_language_server_processes(servers[0])) == 1, "the sessions must share one language server"
    finally:
        for proc in (first, second):
            proc.kill()


def test_a_killed_session_does_not_affect_the_other_sessions(serena_env: tuple[dict[str, str], Path]) -> None:
    env, project_root = serena_env
    first = _start_session(env, project_root, shared_server=True)
    second = _start_session(env, project_root, shared_server=True)
    try:
        first.kill()
        first.wait(timeout=5)

        _call_symbols_overview(second, request_id=3)
    finally:
        second.kill()


def test_the_shared_server_exits_once_it_has_no_sessions(serena_env: tuple[dict[str, str], Path]) -> None:
    env, project_root = serena_env
    proc = _start_session(env, project_root, shared_server=True)
    (server,) = _shared_servers(project_root)
    language_server_processes = _language_server_processes(server)
    assert language_server_processes

    assert proc.stdin is not None
    proc.stdin.close()  # the client ends the session
    proc.wait(timeout=10)

    assert server.is_running(), "the shared server must outlive a session"
    assert not _wait_until_gone([server, *language_server_processes], timeout=30), "the idle shared server did not exit"


def test_project_files_named_like_stdlib_modules_do_not_break_the_shared_server(serena_env: tuple[dict[str, str], Path]) -> None:
    env, project_root = serena_env
    for module in ("json", "socket", "subprocess"):
        (project_root / f"{module}.py").write_text("raise ImportError('the project module was imported instead of the stdlib one')\n")

    proc = _start_session(env, project_root, shared_server=True)
    proc.kill()
