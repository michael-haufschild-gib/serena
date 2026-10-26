import logging
import os.path
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from sensai.util.logging import LogTime

from serena.config.serena_config import ProjectConfig, SerenaPaths
from solidlsp import SolidLanguageServer
from solidlsp.ls_config import LanguageServerConfig, LanguageServerId
from solidlsp.lsp_protocol_handler.lsp_types import DidChangeWatchedFilesParams, FileChangeType, FileEvent
from solidlsp.settings import SolidLSPSettings

if TYPE_CHECKING:
    from .project import Project

log = logging.getLogger(__name__)


class LanguageServerFactory:
    def __init__(
        self,
        project_root: str,
        project_config: ProjectConfig,
        project_data_path: str,
        encoding: str,
        ignored_patterns: list[str],
        ls_timeout: float | None = None,
        ls_specific_settings: dict | None = None,
        trace_lsp_communication: bool = False,
    ):
        self.project_root = project_root
        self.project_config = project_config
        self.project_data_path = project_data_path
        self.encoding = encoding
        self.ignored_patterns = ignored_patterns
        self.ls_timeout = ls_timeout
        self.ls_specific_settings = ls_specific_settings
        self.trace_lsp_communication = trace_lsp_communication

    def create_language_server(self, ls_id: LanguageServerId) -> SolidLanguageServer:
        ls_config = LanguageServerConfig(
            workspace_folders=self.project_config.ls_workspace_folders,
            additional_workspace_folders=self.project_config.ls_additional_workspace_folders,
            ls_id=ls_id,
            ignored_paths=self.ignored_patterns,
            trace_lsp_communication=self.trace_lsp_communication,
            encoding=self.encoding,
        )

        log.info(f"Creating language server instance for {self.project_root}, language={ls_id}.")
        return SolidLanguageServer.create(
            ls_config,
            self.project_root,
            timeout=self.ls_timeout,
            solidlsp_settings=SolidLSPSettings(
                solidlsp_dir=SerenaPaths().serena_user_home_dir,
                project_data_path=self.project_data_path,
                ls_specific_settings=self.ls_specific_settings or {},
            ),
        )


class LanguageServerManager:
    """
    Manages the language servers of a project.

    Language servers are started lazily, on first use, and stopped again once they have not been used for a while
    (see ``idle_timeout``), so that a project costs no language server resources while it is not being worked on.
    """

    _IDLE_CHECK_INTERVAL = 30.0
    """seconds between two checks for idle language servers"""

    def __init__(
        self,
        ls_ids: list[LanguageServerId],
        language_server_factory: LanguageServerFactory,
        project: "Project",
        idle_timeout: float | None = None,
    ) -> None:
        """
        :param ls_ids: the language servers to manage; the first one is the default, which is used for files that
            no language server is responsible for
        :param language_server_factory: the factory with which language servers are created
        :param project: the project the language servers serve
        :param idle_timeout: the number of seconds after which an unused language server is stopped; None to never stop it
        """
        if not ls_ids:
            raise ValueError("At least one language server is required")
        self._ls_ids = list(ls_ids)
        self._language_server_factory = language_server_factory
        self._running: dict[LanguageServerId, SolidLanguageServer] = {}
        self._last_used: dict[LanguageServerId, float] = {}
        self._lock = threading.RLock()
        self._file_change_notifier = LanguageServerFileChangeNotifier(project, self)
        self._idle_timeout = idle_timeout
        self._closed = threading.Event()
        if idle_timeout is not None:
            threading.Thread(target=self._stop_idle_language_servers_periodically, name="LSIdleStopper", daemon=True).start()

    def _stop_idle_language_servers_periodically(self) -> None:
        assert self._idle_timeout is not None
        while not self._closed.wait(self._IDLE_CHECK_INTERVAL):
            self.stop_idle_language_servers(self._idle_timeout)

    def stop_idle_language_servers(self, idle_timeout: float) -> list[LanguageServerId]:
        """
        Stops the language servers that have not been used for the given time; they are restarted on their next use.

        :param idle_timeout: the number of seconds without use after which a language server is stopped
        :return: the language servers that were stopped
        """
        stopped: list[LanguageServerId] = []
        with self._lock:
            now = time.monotonic()
            for ls_id, ls in list(self._running.items()):
                if now - self._last_used.get(ls_id, now) >= idle_timeout:
                    log.info(f"Stopping language server for {ls_id.value}, which has not been used for {idle_timeout:.0f}s")
                    del self._running[ls_id]
                    self._stop_language_server(ls)
                    stopped.append(ls_id)
        return stopped

    def _get_or_start(self, ls_id: LanguageServerId) -> SolidLanguageServer:
        with self._lock:
            ls = self._running.get(ls_id)
            if ls is not None and not ls.is_running():
                log.warning(f"Language server for language {ls_id.value} is not running; restarting ...")
                del self._running[ls_id]
                self._stop_language_server(ls)
                ls = None
            if ls is None:
                # notify the running language servers of pending changes and establish the baseline for the new one
                self._file_change_notifier.poll_and_notify()
                with LogTime(f"Language server startup (language={ls_id.value})", logger=log):
                    ls = self._language_server_factory.create_language_server(ls_id)
                    try:
                        ls.start()
                        if not ls.is_running():
                            raise RuntimeError(f"Failed to start the language server for language {ls_id.value}")
                    except BaseException:
                        self._stop_language_server(ls)
                        raise
                self._running[ls_id] = ls
            self._last_used[ls_id] = time.monotonic()
            return ls

    def _get_responsible_ls_id(self, relative_path: str) -> LanguageServerId | None:
        """:param relative_path: relative path to a file"""
        for ls_id in self._ls_ids:
            if ls_id.get_source_fn_matcher().is_relevant_filename(relative_path):
                return ls_id
        return None

    def get_language_server(self, relative_path: str) -> SolidLanguageServer:
        """
        :param relative_path: relative path to a file
        :return: the (running) language server responsible for the file, started if necessary; the default
            language server if no language server is responsible for the file
        """
        if len(self._ls_ids) > 1 and os.path.isdir(relative_path):
            raise ValueError(f"Expected a file path, but got a directory: {relative_path}")
        return self._get_or_start(self._get_responsible_ls_id(relative_path) or self._ls_ids[0])

    def restart_language_server(self, ls_id: LanguageServerId) -> SolidLanguageServer:
        """
        Forces recreation and restart of the language server for the given language.

        :param ls_id: the language
        :return: the newly started language server
        """
        if ls_id not in self._ls_ids:
            raise ValueError(f"No language server for language {ls_id.value} present; cannot restart")
        with self._lock:
            ls = self._running.pop(ls_id, None)
            if ls is not None:
                self._stop_language_server(ls)
            return self._get_or_start(ls_id)

    def add_language_server(self, ls_id: LanguageServerId) -> SolidLanguageServer:
        """
        Adds a new language server for the given language and starts it.

        :param ls_id: the language server to add
        :return: the started language server
        """
        with self._lock:
            if ls_id in self._ls_ids:
                raise ValueError(f"Language server for language {ls_id.value} already present")
            self._ls_ids.append(ls_id)
            return self._get_or_start(ls_id)

    def remove_language_server(self, ls_id: LanguageServerId) -> None:
        """
        Removes the language server for the given language, stopping it if it is running.

        :param ls_id: the language
        """
        with self._lock:
            if ls_id not in self._ls_ids:
                raise ValueError(f"No language server for language {ls_id.value} present; cannot remove")
            self._ls_ids.remove(ls_id)
            ls = self._running.pop(ls_id, None)
            if ls is not None:
                self._stop_language_server(ls)

    def get_active_language_server_ids(self) -> list[LanguageServerId]:
        """
        :return: the languages whose language servers are managed (whether currently running or not)
        """
        return list(self._ls_ids)

    def get_running_language_server_ids(self) -> list[LanguageServerId]:
        """
        :return: the languages whose language servers are currently running
        """
        with self._lock:
            return list(self._running)

    @staticmethod
    def _stop_language_server(ls: SolidLanguageServer, timeout: float = 2.0) -> None:
        """
        Stops the given language server for good, closing its (persistent) symbol caches.
        """
        if ls.is_running():
            log.info(f"Stopping language server for language {ls.ls_id} ...")
            ls.stop(shutdown_timeout=timeout)
        ls.close_caches()

    def iter_language_servers(self) -> Iterator[SolidLanguageServer]:
        """
        :return: all managed language servers, each started if necessary
        """
        for ls_id in list(self._ls_ids):
            yield self._get_or_start(ls_id)

    def iter_running_language_servers(self) -> list[SolidLanguageServer]:
        """
        :return: the language servers that are currently running (without starting any)
        """
        with self._lock:
            return list(self._running.values())

    def stop_all(self, timeout: float = 2.0) -> None:
        """
        Stops all running language servers and the idle check; the manager must not be used afterwards.

        :param timeout: timeout for shutdown of each language server
        """
        self._closed.set()
        with self._lock:
            for ls in self._running.values():
                self._stop_language_server(ls, timeout=timeout)
            self._running.clear()

    def has_suitable_ls_for_file(self, relative_file_path: str) -> bool:
        return self._get_responsible_ls_id(relative_file_path) is not None

    def sync_file_system_changes(self) -> int:
        """
        Polls the file system for changes to source files and notifies the running language servers of any changes
        (particularly changes that happened outside of Serena's own file tools, which are not covered by
        the notifications sent by those tools/CodeEditor).
        Does nothing if no language server is running: a language server that is started later reads the current state.

        :return: the number of individual file change events detected (0 if nothing changed).
        """
        if not self.iter_running_language_servers():
            return 0
        log.info("Polling file system for changes to source files ...")
        num_changes = self._file_change_notifier.poll_and_notify()
        log.info(f"File system polling complete; {num_changes} change events sent to language servers.")
        return num_changes


class LanguageServerFileChangeNotifier:
    """
    Detects changes to source files on disk and notifies language servers of those changes.
    """

    def __init__(self, project: "Project", language_server_manager: LanguageServerManager) -> None:
        """
        No baseline is established on construction; the first call of :meth:`poll_and_notify` does that.
        """
        self._project = project
        self._language_server_manager = language_server_manager
        self._freshness_last_seen_mtimes: dict[str, float] | None = None
        self._freshness_lock = threading.Lock()

    def poll_and_notify(self) -> int:
        """
        Detects source files that were changed, created or deleted on disk since the last call
        and notifies every language server managed for this project via the LSP
        ``workspace/didChangeWatchedFiles`` notification.

        This exists because Serena's own file and symbol tools notify the language server inline
        (via didOpen/didChange/didClose) when they edit a file, but edits made through any other
        channel (another editor, a second agent, a git checkout, a build step) are otherwise
        invisible to a warm language server, causing symbolic queries to answer from a stale index.

        The set of files considered is exactly the set Serena itself tracks (see
        :meth:`gather_source_files`), so no separate file-discovery logic has to be kept in sync.
        The dominant cost is the directory walk plus one ``os.stat`` per tracked file; this is
        intended to be called before symbolic tool invocations rather than on a timer.

        :return: the number of change events sent (0 if nothing changed or on the first call, which only
            establishes the baseline).
        """
        current: dict[str, float] = {}
        for rel_path in self._project.gather_source_files():
            try:
                current[rel_path] = os.stat(os.path.join(self._project.project_root, rel_path)).st_mtime
            except OSError:
                continue

        # Read-diff-swap under the lock only; the filesystem walk above and the LSP notifications
        # below stay outside it so concurrent callers do not serialize on I/O.
        with self._freshness_lock:
            previous = self._freshness_last_seen_mtimes
            self._freshness_last_seen_mtimes = current

            if previous is None:
                return 0

            # compute the set of individual events (created, changed, deleted)
            events: list[tuple[str, FileChangeType]] = []
            for rel_path, mtime in current.items():
                prev_mtime = previous.get(rel_path)
                if prev_mtime is None:
                    events.append((rel_path, FileChangeType.Created))
                elif mtime > prev_mtime:
                    events.append((rel_path, FileChangeType.Changed))
            events.extend((rel_path, FileChangeType.Deleted) for rel_path in previous if rel_path not in current)

        if not events:
            return 0

        # create the change didChangeWatchedFiles notification
        changes: list[FileEvent] = [
            {"uri": Path(self._project.project_root, rel_path).resolve().as_uri(), "type": change_type} for rel_path, change_type in events
        ]
        params: DidChangeWatchedFilesParams = {"changes": changes}
        created_paths = [rel_path for rel_path, change_type in events if change_type == FileChangeType.Created]

        for ls in self._language_server_manager.iter_running_language_servers():
            # send the didChangeWatchedFiles notification to the language server
            try:
                ls.server.notify.did_change_watched_files(params)
            except Exception as e:
                log.error("Failed to notify language server of watched file changes", exc_info=e)

            # A didChangeWatchedFiles(Created) notification alone is not enough for every backend
            # (observed with pyright) to fold a brand-new file into its cross-file reference graph;
            # an open/close cycle forces the parse+bind that Serena's own file tools trigger via
            # SolidLanguageServer.open_file().
            for rel_path in created_paths:
                if ls.is_ignored_path(rel_path, ignore_unsupported_files=True):
                    continue
                try:
                    with ls.open_file(rel_path):
                        pass
                except Exception as e:
                    log.error(f"Failed to refresh newly created file {rel_path!r} in language server", exc_info=e)

        return len(events)
