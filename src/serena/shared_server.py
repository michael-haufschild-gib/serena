"""
Client side of the shared per-project Serena server.

Every MCP client session (chat) runs ``serena start-mcp-server``. For a project, that process replaces itself
(via exec) with the lightweight launcher in this module, which connects to the project's shared server (see
:mod:`serena.project_daemon`), starting it if necessary, and relays the MCP messages between the client's stdio
and the server. All sessions on a project thereby share one set of language servers.

This module must only import the standard library (and :mod:`serena.util.process_lifecycle`, which does the
same), as the launcher runs once per chat and is meant to be tiny.
"""

import fcntl
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from serena.util.process_lifecycle import ProcessLifecycle


@dataclass
class SharedServerRequest:
    """
    The parameters of an MCP session that a launcher requests from the shared server of a project.
    """

    project_root: str
    context: str
    modes: list[str] = field(default_factory=list)
    added_modes: list[str] = field(default_factory=list)
    log_level: str | None = None
    trace_lsp_communication: bool | None = None
    tool_timeout: float | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, s: str) -> "SharedServerRequest":
        return cls(**json.loads(s))


class SharedServerAddress:
    """
    The rendezvous point of the shared server of a project: its socket, the lock that serialises starting it,
    and the code it runs.

    The address depends on the installed Serena code and on the Serena home directory, so after an upgrade new
    sessions get a new server (old servers exit once their sessions are gone), and differently configured Serena
    installations never share a server.
    """

    def __init__(self, project_root: str) -> None:
        self.project_root = os.path.realpath(project_root)
        code_location = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        serena_home = os.getenv("SERENA_HOME") or os.path.expanduser("~/.serena")
        key = "\0".join([self.project_root, code_location, _code_fingerprint(code_location), serena_home])
        digest = hashlib.sha256(key.encode()).hexdigest()[:20]
        # sockets go to the per-user temp directory, as socket paths are limited to 104 bytes on macOS
        run_dir = Path(tempfile.gettempdir()) / "serena-shared-servers"
        run_dir.mkdir(mode=0o700, exist_ok=True)
        self.socket_path = str(run_dir / f"{digest}.sock")
        self.lock_path = self.lock_path_for(self.socket_path)

    @staticmethod
    def lock_path_for(socket_path: str) -> str:
        """
        :param socket_path: the socket path of a shared server
        :return: the path of the lock under which launchers connect to (or start) that server
        """
        return os.path.splitext(socket_path)[0] + ".lock"

    def try_connect(self) -> socket.socket | None:
        """
        :return: a connection to the running server, or None if no server is running
        """
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(self.socket_path)
            return sock
        except OSError:
            sock.close()
            return None


def _code_fingerprint(code_location: str) -> str:
    """
    :return: a fingerprint that changes whenever the Serena code changes, given the directory containing the packages
    """
    latest_mtime = 0.0
    for package in ("serena", "solidlsp", "interprompt"):
        for dirpath, _dirnames, filenames in os.walk(os.path.join(code_location, package)):
            for filename in filenames:
                if filename.endswith(".py"):
                    latest_mtime = max(latest_mtime, os.stat(os.path.join(dirpath, filename)).st_mtime)
    return str(latest_mtime)


class SharedServerLauncher:
    """
    Connects an MCP client's stdio to the shared server of a project, starting the server if necessary.
    """

    SERVER_STARTUP_TIMEOUT = 60.0

    def __init__(self, request: SharedServerRequest) -> None:
        self._request = request
        self._address = SharedServerAddress(request.project_root)

    def _connect_or_start_server(self) -> socket.socket:
        # the lock ensures that only one launcher starts a server; the others wait for it and then connect
        with open(self._address.lock_path, "w") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            sock = self._address.try_connect()
            if sock is not None:
                return sock
            if os.path.exists(self._address.socket_path):
                os.unlink(self._address.socket_path)  # stale socket of a server that is gone
            server = subprocess.Popen(
                # -P: never import modules from the project directory (the server's working directory)
                [sys.executable, "-P", "-m", "serena.project_daemon", self._address.project_root, self._address.socket_path],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,  # the server outlives this session
                cwd=self._address.project_root,
            )
            deadline = time.monotonic() + self.SERVER_STARTUP_TIMEOUT
            while time.monotonic() < deadline:
                sock = self._address.try_connect()
                if sock is not None:
                    return sock
                if server.poll() is not None:
                    raise RuntimeError(f"The shared Serena server for {self._address.project_root} exited during startup; see its log")
                time.sleep(0.05)
            raise RuntimeError(f"The shared Serena server for {self._address.project_root} did not start in time; see its log")

    def run(self) -> int:
        """
        Relays the MCP session until either side closes it.

        :return: the exit code for the process
        """
        sock = self._connect_or_start_server()
        sock.sendall((self._request.to_json() + "\n").encode())

        lifecycle = ProcessLifecycle()
        lifecycle.add_shutdown_action(lambda: _close_quietly(sock))
        lifecycle.install()

        def forward_stdin() -> None:
            while True:
                data = os.read(0, 65536)
                if not data:
                    break
                sock.sendall(data)
            # the client closed its end: let the server end the session, then exit once it has
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass

        threading.Thread(target=forward_stdin, name="ForwardStdin", daemon=True).start()
        while True:
            try:
                data = sock.recv(65536)
            except OSError:
                break
            if not data:
                break
            _write_fully(1, data)
        lifecycle.shutdown("the shared server closed the session")
        return 0


def _write_fully(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def _close_quietly(sock: socket.socket) -> None:
    try:
        sock.close()
    except OSError:
        pass


def main() -> None:
    """
    Entry point: ``python -m serena.shared_server <request JSON>``.
    """
    sys.exit(SharedServerLauncher(SharedServerRequest.from_json(sys.argv[1])).run())


if __name__ == "__main__":
    main()
