"""Regression tests for the TypeScript server's timeout policy.

The TypeScript server is permissive on readiness and indexing timeouts (log and proceed).
These tests pin that policy and the settings plumbing directly, without spawning language
server processes.
"""

import threading
import time

import pytest

from solidlsp.language_servers.typescript_language_server import TypeScriptLanguageServer, TypeScriptServerCrashedError
from solidlsp.settings import SolidLSPSettings


def _bare_ts_server(cls: type[TypeScriptLanguageServer], custom_settings: dict | None = None) -> TypeScriptLanguageServer:
    """Create an instance without running __init__ (no process, no repo scan), setting only the
    state the timeout machinery touches; same technique as test_rename_didopen.py.
    """
    server = object.__new__(cls)
    server.server_ready = threading.Event()
    server._progress_lock = threading.Lock()
    server._active_progress_tokens = set()
    server._indexing_complete = threading.Event()
    server._indexing_complete.set()  # mirrors __init__: initially no active work
    server._crash_message = None  # mirrors __init__: no crash observed yet
    server._custom_settings = SolidLSPSettings.CustomLSSettings(custom_settings)
    return server


class TestBaseTypeScriptTimeoutPolicy:
    """The server must stay permissive on timeouts."""

    def test_server_ready_timeout_proceeds(self) -> None:
        server = _bare_ts_server(TypeScriptLanguageServer)
        assert not server.server_ready.is_set()

        server._handle_server_ready_timeout(10.0)  # must not raise

        assert server.server_ready.is_set()

    def test_indexing_timeout_proceeds(self) -> None:
        server = _bare_ts_server(TypeScriptLanguageServer)
        server.expect_indexing()
        server._active_progress_tokens.add("indexing-1")

        server._handle_project_indexing_timeout(30.0)  # must not raise

    def test_timeout_defaults(self) -> None:
        server = _bare_ts_server(TypeScriptLanguageServer)
        assert server._get_server_ready_timeout() == 10.0
        assert server._get_indexing_timeout() == 30.0

    def test_timeouts_configurable_via_ls_specific_settings(self) -> None:
        server = _bare_ts_server(TypeScriptLanguageServer, {"server_ready_timeout": 1.5, "indexing_timeout": 2.5})
        assert server._get_server_ready_timeout() == 1.5
        assert server._get_indexing_timeout() == 2.5

    def test_indexing_start_grace_default(self) -> None:
        server = _bare_ts_server(TypeScriptLanguageServer)
        assert server._get_indexing_start_grace() == 5.0

    def test_indexing_start_grace_configurable_via_ls_specific_settings(self) -> None:
        server = _bare_ts_server(TypeScriptLanguageServer, {"indexing_start_grace": 12.0})
        assert server._get_indexing_start_grace() == 12.0


class TestWaitForIndexingStartOrCompletion:
    def test_returns_false_when_active_progress_never_completes(self) -> None:
        server = _bare_ts_server(TypeScriptLanguageServer)
        server.expect_indexing()
        server._active_progress_tokens.add("stuck-progress")

        assert server._wait_for_indexing_start_or_completion(timeout=0.1, start_grace=0.0) is False

    def test_treats_absent_progress_as_ready(self) -> None:
        server = _bare_ts_server(TypeScriptLanguageServer)
        server.expect_indexing()

        assert server._wait_for_indexing_start_or_completion(timeout=30.0, start_grace=0.0) is True
        assert server._indexing_complete.is_set()

    def test_returns_immediately_when_no_indexing_expected(self) -> None:
        # _indexing_complete is initially set; a long grace must not block in that case
        server = _bare_ts_server(TypeScriptLanguageServer)

        start = time.monotonic()
        assert server._wait_for_indexing_start_or_completion(timeout=30.0, start_grace=30.0) is True
        # must return via the early is_set() check, not sit out the 30s grace; the path is
        # pure in-memory, so a 10s bound cannot flake on a slow runner
        assert time.monotonic() - start < 10.0

    def test_returns_true_when_progress_completes(self) -> None:
        server = _bare_ts_server(TypeScriptLanguageServer)
        server.expect_indexing()
        server._active_progress_tokens.add("indexing-1")

        def finish_indexing() -> None:
            with server._progress_lock:
                server._active_progress_tokens.clear()
            server._indexing_complete.set()

        timer = threading.Timer(0.05, finish_indexing)
        timer.start()
        try:
            # both timer orderings pass: an early fire takes the absent-progress branch,
            # which sets the event itself, while a late fire wakes Event.wait; ordering
            # cannot flake this test. It fails only if the spawned thread does not run
            # within the 30s ceiling, which means the runner is hung.
            assert server._wait_for_indexing_start_or_completion(timeout=30.0, start_grace=0.0) is True
        finally:
            timer.cancel()


class TestTsserverCrashDetection:
    """oraios/serena#1814: typescript-language-server sends a $/progress "end" for the
    in-flight token as part of tearing its connection down after tsserver dies, which
    _wait_for_indexing_start_or_completion previously could not distinguish from a real
    completion: find_referencing_symbols returned {} with isError: false instead of
    surfacing the crash. window/logMessage does carry the crash independently; these
    tests drive the exact begin/crash-log/end sequence from the reporter's log and pin
    that every wait path now raises instead of reporting silent success.
    """

    def test_tsserver_exit_message_matches_sigabrt_line(self) -> None:
        msg = {"type": 1, "message": "[lspserver] [tsclient] [tsserver] Exited. Code: null. Signal: SIGABRT"}
        assert TypeScriptLanguageServer._tsserver_exit_message(msg) == msg["message"]

    def test_tsserver_exit_message_ignores_non_error_type(self) -> None:
        # type 3 (Info) is what a clean shutdown/log line would carry, not type 1 (Error)
        msg = {"type": 3, "message": "[lspserver] [tsclient] [tsserver] Exited. Code: null. Signal: SIGABRT"}
        assert TypeScriptLanguageServer._tsserver_exit_message(msg) is None

    def test_tsserver_exit_message_ignores_unrelated_error(self) -> None:
        msg = {"type": 1, "message": "Some other tsserver error unrelated to process exit"}
        assert TypeScriptLanguageServer._tsserver_exit_message(msg) is None

    def test_raise_if_crashed_is_a_noop_when_no_crash_observed(self) -> None:
        server = _bare_ts_server(TypeScriptLanguageServer)
        server._raise_if_crashed()  # must not raise

    def test_raise_if_crashed_raises_with_recorded_message(self) -> None:
        server = _bare_ts_server(TypeScriptLanguageServer)
        server._crash_message = "tsserver exited abnormally: [tsserver] Exited. Code: null. Signal: SIGABRT"

        with pytest.raises(TypeScriptServerCrashedError, match="SIGABRT"):
            server._raise_if_crashed()

    def test_wait_for_indexing_raises_when_progress_end_follows_a_crash(self) -> None:
        """Drives the reporter's exact sequence: $/progress begin, the SIGABRT window/logMessage,
        then $/progress end (teardown), the same "end" that previously read as success.
        """
        server = _bare_ts_server(TypeScriptLanguageServer)
        server.expect_indexing()
        server._active_progress_tokens.add("646a4e19")

        # window/logMessage handler's effect: record the crash independently of progress state
        server._crash_message = "tsserver exited abnormally: [tsserver] Exited. Code: null. Signal: SIGABRT"
        # $/progress end (teardown): drains the token and sets indexing_complete, exactly as a
        # real completion would
        server._active_progress_tokens.discard("646a4e19")
        server._indexing_complete.set()

        with pytest.raises(TypeScriptServerCrashedError, match="SIGABRT"):
            server.wait_for_indexing(timeout=1.0)

    def test_wait_for_indexing_returns_true_on_clean_completion(self) -> None:
        """Same $/progress begin/end shape as the crash test, without a crash message: must still
        return True, not regress the ordinary success path.
        """
        server = _bare_ts_server(TypeScriptLanguageServer)
        server.expect_indexing()
        server._active_progress_tokens.add("646a4e19")
        server._active_progress_tokens.discard("646a4e19")
        server._indexing_complete.set()

        assert server.wait_for_indexing(timeout=1.0) is True

    def test_wait_for_indexing_start_or_completion_raises_via_early_is_set_branch(self) -> None:
        """Covers the early "if self._indexing_complete.is_set(): return True" branch, which is
        reached (not the tail wait_for_indexing() call) when begin+crash+end all land before the
        first poll iteration observes an active token.
        """
        server = _bare_ts_server(TypeScriptLanguageServer)
        server.expect_indexing()
        server._crash_message = "tsserver exited abnormally: [tsserver] Exited. Code: null. Signal: SIGABRT"
        server._indexing_complete.set()  # progress already drained by the time we start waiting

        with pytest.raises(TypeScriptServerCrashedError, match="SIGABRT"):
            server._wait_for_indexing_start_or_completion(timeout=1.0, start_grace=1.0)

    def test_wait_for_indexing_start_or_completion_raises_via_absent_progress_branch(self) -> None:
        """Covers the "treat absent progress as ready" branch: no active token observed within the
        start grace, but a crash was already recorded.
        """
        server = _bare_ts_server(TypeScriptLanguageServer)
        server.expect_indexing()
        server._crash_message = "tsserver exited abnormally: [tsserver] Exited. Code: null. Signal: SIGABRT"

        with pytest.raises(TypeScriptServerCrashedError, match="SIGABRT"):
            server._wait_for_indexing_start_or_completion(timeout=1.0, start_grace=0.05)

    def test_wait_for_cross_file_references_propagates_the_crash(self) -> None:
        """The actual call path find_referencing_symbols drives (ls.py's SymbolLocationRequest.execute
        calls this with no surrounding try/except) must raise, not silently log "indexing complete"
        and let the caller receive an empty result.
        """
        server = _bare_ts_server(TypeScriptLanguageServer)
        server._has_waited_for_cross_file_references = False
        server.expect_indexing()
        server._crash_message = "tsserver exited abnormally: [tsserver] Exited. Code: null. Signal: SIGABRT"

        with pytest.raises(TypeScriptServerCrashedError, match="SIGABRT"):
            server._wait_for_cross_file_references_if_needed()

        # the crash must be reported, not swallowed into a false "we've already waited" state
        assert server._has_waited_for_cross_file_references is False


class TestWaitForCrossFileReferencesUsesConfiguredGrace:
    """The actual find-references call path (not just the helper in isolation) must honor
    indexing_start_grace: this is the mechanism behind oraios/serena#1586, where a large
    project's tsserver takes longer than the (previously hardcoded, unconfigurable) grace to
    even start reporting progress, so the first cross-file reference query silently races a
    still-loading project and returns incomplete results.
    """

    def test_configured_grace_bounds_the_real_call_path(self) -> None:
        server = _bare_ts_server(TypeScriptLanguageServer, {"indexing_start_grace": 0.05})
        server._has_waited_for_cross_file_references = False
        server.expect_indexing()  # mirrors _pre_open_for_cross_file_references having already run

        start = time.monotonic()
        server._wait_for_cross_file_references_if_needed()
        elapsed = time.monotonic() - start

        assert server._has_waited_for_cross_file_references is True
        # bounded by the configured 0.05s grace, not the 5.0s default (let alone the old 2.0s one)
        assert elapsed < 1.0

    def test_second_call_is_a_noop_regardless_of_grace(self) -> None:
        server = _bare_ts_server(TypeScriptLanguageServer, {"indexing_start_grace": 0.05})
        server._has_waited_for_cross_file_references = True

        start = time.monotonic()
        server._wait_for_cross_file_references_if_needed()
        assert time.monotonic() - start < 0.1
