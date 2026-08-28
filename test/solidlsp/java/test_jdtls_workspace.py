"""
Tests for the isolation and self-healing guarantees that :class:`JdtlsWorkspace` provides for the
Eclipse workspace directories used by JDTLS.

These tests do not start JDTLS and do not require Java to be installed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from solidlsp.language_servers.eclipse_jdtls_workspace import JdtlsWorkspace

WORKSPACE_KEY = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def readonly_config(tmp_path: Path) -> str:
    """
    A stand-in for the pristine OSGi configuration directory shipped with JDTLS.
    """
    config_dir = tmp_path / "readonly_config"
    (config_dir / "org.eclipse.osgi").mkdir(parents=True)
    (config_dir / "config.ini").write_text("osgi.bundles=")
    return str(config_dir)


@pytest.fixture
def workspaces_root(tmp_path: Path) -> str:
    return str(tmp_path / "workspaces")


def _resources_state_dir(workspace: JdtlsWorkspace) -> Path:
    state_dir = Path(workspace.data_dir) / ".metadata" / ".plugins" / "org.eclipse.core.resources"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def _acquire_in_killed_process(workspaces_root: str, readonly_config: str) -> str:
    """
    Acquires a workspace in a separate process and kills that process without letting it release
    the workspace, reproducing what a crashed Serena instance leaves behind.

    :return: the data directory of the workspace the killed process had acquired.
    """
    script = (
        "import sys, time\n"
        "from solidlsp.language_servers.eclipse_jdtls_workspace import JdtlsWorkspace\n"
        f"w = JdtlsWorkspace.acquire({workspaces_root!r}, {WORKSPACE_KEY!r}, {readonly_config!r})\n"
        "print(w.data_dir, flush=True)\n"
        "time.sleep(60)\n"
    )
    process = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout is not None
        data_dir = process.stdout.readline().strip()
        assert data_dir, "child process did not report an acquired workspace"
    finally:
        process.kill()
        process.wait(timeout=30)
    return data_dir


class TestWorkspaceIsolation:
    def test_concurrent_acquisitions_get_separate_directories(self, workspaces_root: str, readonly_config: str) -> None:
        """
        Two processes working on the same project at the same time must not share Eclipse state.
        """
        first = JdtlsWorkspace.acquire(workspaces_root, WORKSPACE_KEY, readonly_config)
        second = JdtlsWorkspace.acquire(workspaces_root, WORKSPACE_KEY, readonly_config)
        try:
            assert first.data_dir != second.data_dir
            assert first.config_path != second.config_path
            for workspace in (first, second):
                assert Path(workspace.data_dir).is_dir()
                assert (Path(workspace.config_path) / "config.ini").is_file()
        finally:
            second.release()
            first.release()

    def test_released_workspace_is_reused_by_the_next_acquisition(self, workspaces_root: str, readonly_config: str) -> None:
        """
        Sequential sessions on a project reuse one workspace, so the Eclipse index stays warm.
        """
        first = JdtlsWorkspace.acquire(workspaces_root, WORKSPACE_KEY, readonly_config)
        first_data_dir = first.data_dir
        first.release()

        second = JdtlsWorkspace.acquire(workspaces_root, WORKSPACE_KEY, readonly_config)
        try:
            assert second.data_dir == first_data_dir
        finally:
            second.release()

    def test_isolated_workspace_does_not_outlive_its_session(self, workspaces_root: str, readonly_config: str) -> None:
        """
        The extra workspaces created under contention must not accumulate on disk.
        """
        held = JdtlsWorkspace.acquire(workspaces_root, WORKSPACE_KEY, readonly_config)
        isolated = JdtlsWorkspace.acquire(workspaces_root, WORKSPACE_KEY, readonly_config)
        isolated_root = Path(isolated.data_dir).parent
        try:
            assert isolated_root.is_dir()
            isolated.release()
            assert not isolated_root.exists()
        finally:
            held.release()

    def test_workspace_abandoned_by_a_killed_process_is_cleaned_up(self, workspaces_root: str, readonly_config: str) -> None:
        """
        A process that is killed before it can release its isolated workspace must not leak it.
        """
        held = JdtlsWorkspace.acquire(workspaces_root, WORKSPACE_KEY, readonly_config)
        try:
            abandoned_root = Path(_acquire_in_killed_process(workspaces_root, readonly_config)).parent
            assert abandoned_root.is_dir()

            JdtlsWorkspace.acquire(workspaces_root, WORKSPACE_KEY, readonly_config).release()

            assert not abandoned_root.exists()
        finally:
            held.release()


class TestWorkspaceRecovery:
    def test_unsaved_state_from_a_previous_session_is_discarded(self, workspaces_root: str, readonly_config: str) -> None:
        """
        Snapshots left behind by a terminated session abort Eclipse's startup once the resources
        they reference are gone, so a new session must not inherit them.
        """
        workspace = JdtlsWorkspace.acquire(workspaces_root, WORKSPACE_KEY, readonly_config)
        try:
            state_dir = _resources_state_dir(workspace)
            snapshot = state_dir / "9.snap"
            snapshot.write_bytes(b"stale resource tree delta")
            saved_tree = state_dir / ".root"
            saved_tree.mkdir()

            workspace.discard_unsaved_state()

            assert not snapshot.exists()
            assert saved_tree.is_dir(), "the saved workspace tree must be kept"
        finally:
            workspace.release()

    def test_discarding_unsaved_state_is_applicable_to_an_unused_workspace(self, workspaces_root: str, readonly_config: str) -> None:
        """
        A workspace that Eclipse has never written to must be usable without special-casing.
        """
        workspace = JdtlsWorkspace.acquire(workspaces_root, WORKSPACE_KEY, readonly_config)
        try:
            workspace.discard_unsaved_state()
            assert Path(workspace.data_dir).is_dir()
        finally:
            workspace.release()

    def test_reset_discards_eclipse_state_and_restores_a_usable_workspace(self, workspaces_root: str, readonly_config: str) -> None:
        """
        Corruption that outlives the discarding of unsaved state is resolved by a full reset.
        """
        workspace = JdtlsWorkspace.acquire(workspaces_root, WORKSPACE_KEY, readonly_config)
        try:
            _resources_state_dir(workspace).joinpath("9.snap").write_bytes(b"corrupt")
            Path(workspace.config_path, "org.eclipse.osgi", "damaged").write_text("x")
            imported_project = Path(workspace.data_dir, "jdt.ls-java-project")
            imported_project.mkdir()

            workspace.reset(readonly_config)

            assert not Path(workspace.data_dir, ".metadata").exists()
            assert not Path(workspace.config_path, "org.eclipse.osgi", "damaged").exists()
            assert (Path(workspace.config_path) / "config.ini").is_file()
            assert Path(workspace.data_dir).is_dir()
        finally:
            workspace.release()
