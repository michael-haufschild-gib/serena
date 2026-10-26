"""
The shared Serena server of a project, which serves the MCP sessions of all chats working on the project, so that
they share a single set of language servers (see :mod:`serena.shared_server` for the client side).

Started by the first launcher for the project as ``python -m serena.project_daemon <project root> <socket path>``,
it runs detached and exits once it has not had a session for ``ls_idle_timeout`` seconds.
"""

import fcntl
import io
import logging
import os
import select
import socket
import sys
import threading
import time

import anyio
from mcp.server.stdio import stdio_server
from sensai.util.logging import LogTime

from serena.config.serena_config import ModeSelectionDefinitionWithAddedModes, SerenaConfig, SerenaPaths
from serena.constants import SERENA_LOG_FORMAT
from serena.mcp import SerenaMCPFactory
from serena.project import Project
from serena.shared_server import SharedServerAddress, SharedServerRequest
from serena.task_executor import TaskExecutor
from serena.util.process_lifecycle import ProcessLifecycle

log = logging.getLogger(__name__)


class SharedProjectServer:
    """
    Serves MCP sessions for one project over a Unix socket, giving each session its own agent (with the session's
    context and modes) on top of a shared project and a shared task executor.
    """

    _IDLE_CHECK_INTERVAL = 30.0

    def __init__(self, project_root: str, socket_path: str) -> None:
        self._socket_path = socket_path
        self._serena_config = SerenaConfig.from_config_file()
        self._project = self._load_project(project_root)
        self._task_executor = TaskExecutor("SharedProjectTaskExecutor")
        self._sessions_lock = threading.Lock()
        self._num_sessions = 0
        self._last_session_end = time.monotonic()
        idle_timeout = self._serena_config.ls_idle_timeout
        self._idle_timeout: float | None = idle_timeout if idle_timeout > 0 else None
        self._lifecycle = ProcessLifecycle()
        self._lifecycle.add_shutdown_action(self._shut_down)

    def _load_project(self, project_root: str) -> Project:
        project = self._serena_config.get_project(project_root)
        if project is None:
            project = self._serena_config.add_project_from_path(project_root, asynchronous_autogen=True)
        return project

    def _shut_down(self) -> None:
        try:
            os.unlink(self._socket_path)
        except FileNotFoundError:
            pass
        self._project.shutdown()

    def serve(self) -> None:
        """
        Accepts and serves sessions until the server has been idle for too long or is terminated.
        """
        self._lifecycle.install(watch_parent=False)  # the launcher that started us is just one of our clients

        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(self._socket_path)
        listener.listen()
        log.info(f"Serving project {self._project.project_root} at {self._socket_path}")

        # accepting connections and deciding to exit when idle happen in this one loop, so they cannot race
        listener.settimeout(
            self._IDLE_CHECK_INTERVAL if self._idle_timeout is None else min(self._IDLE_CHECK_INTERVAL, self._idle_timeout / 2)
        )
        while True:
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                if self._is_idle() and self._exit_if_still_idle(listener):
                    return
                continue
            connection.settimeout(None)
            with self._sessions_lock:
                self._num_sessions += 1
            threading.Thread(target=self._serve_session, args=(connection,), name="Session", daemon=True).start()

    def _is_idle(self) -> bool:
        if self._idle_timeout is None:
            return False
        with self._sessions_lock:
            return self._num_sessions == 0 and time.monotonic() - self._last_session_end >= self._idle_timeout

    def _exit_if_still_idle(self, listener: socket.socket) -> bool:
        """
        Exits unless a launcher connected in the meantime. Launchers connect only while holding the lock file, so
        holding it here means that every connection made so far is already waiting to be accepted.

        :return: False if the server must keep running (it exits otherwise)
        """
        with open(SharedServerAddress.lock_path_for(self._socket_path), "w") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            if select.select([listener], [], [], 0)[0]:
                return False  # a connection is waiting
            listener.close()
            # the socket is removed and the process exits while the lock is held, so the next launcher starts a new server
            self._lifecycle.shutdown(f"no session for {self._idle_timeout:.0f}s")
        return True

    def _serve_session(self, connection: socket.socket) -> None:
        try:
            with connection, connection.makefile("rb") as reader, connection.makefile("wb") as writer:
                request = SharedServerRequest.from_json(reader.readline().decode())
                log.info(f"New session: {request}")
                with LogTime("Session", logger=log):
                    anyio.run(self._run_session, request, reader, writer)
        except Exception as e:
            log.error(f"Session failed: {e}", exc_info=e)
        finally:
            with self._sessions_lock:
                self._num_sessions -= 1
                self._last_session_end = time.monotonic()

    async def _run_session(self, request: SharedServerRequest, reader: io.BufferedReader, writer: io.BufferedWriter) -> None:
        mode_selection_def = None
        if request.modes or request.added_modes:
            mode_selection_def = ModeSelectionDefinitionWithAddedModes(
                default_modes=request.modes or None, added_modes=request.added_modes or None
            )
        factory = SerenaMCPFactory(context=request.context, shared_project=self._project, task_executor=self._task_executor)
        mcp = factory.create_mcp_server(
            mode_selection_def=mode_selection_def,
            log_level=request.log_level,  # type: ignore[arg-type]
            trace_lsp_communication=request.trace_lsp_communication,
            tool_timeout=request.tool_timeout,
        )
        stdin = anyio.wrap_file(io.TextIOWrapper(reader, encoding="utf-8", errors="replace"))
        stdout = anyio.wrap_file(io.TextIOWrapper(writer, encoding="utf-8"))
        async with stdio_server(stdin, stdout) as (read_stream, write_stream):
            # noinspection PyProtectedMember
            await mcp._mcp_server.run(read_stream, write_stream, mcp._mcp_server.create_initialization_options())


def main() -> None:
    project_root, socket_path = sys.argv[1], sys.argv[2]

    logging.basicConfig(
        level=logging.INFO,
        format=SERENA_LOG_FORMAT,
        filename=SerenaPaths().get_next_log_file_path("shared-server"),
    )
    try:
        SharedProjectServer(project_root, socket_path).serve()
    except Exception as e:
        log.error(f"Shared server for {project_root} failed: {e}", exc_info=e)
        raise


if __name__ == "__main__":
    main()
