"""
Management of the on-disk Eclipse workspace and OSGi configuration directories used by JDTLS.
"""

import logging
import os
import shutil
import uuid
from pathlib import Path, PurePath
from typing import IO

log = logging.getLogger(__name__)


class _ExclusiveFileLock:
    """
    An advisory, OS-level exclusive lock on a file, used to establish sole ownership of the
    directory containing it.

    The lock is held for as long as the owning process keeps the file handle open and is
    released by the operating system if the process dies, which makes it safe against
    crashed processes leaving stale locks behind.
    """

    def __init__(self, path: str) -> None:
        """
        :param path: path of the lock file; its parent directory must exist.
        """
        self._path = path
        self._handle: IO[bytes] | None = None

    @property
    def path(self) -> str:
        return self._path

    def try_acquire(self) -> bool:
        """
        Attempts to acquire the lock without blocking.

        :return: True if the lock is now held by this instance, False if it is held elsewhere.
        """
        # the handle must outlive this call: the lock is held exactly as long as it stays open
        handle = open(self._path, "a+b")  # noqa: SIM115
        try:
            self._lock_handle(handle)
        except OSError:
            handle.close()
            return False
        except Exception:
            handle.close()
            raise
        self._handle = handle
        return True

    def release(self) -> None:
        """
        Releases the lock if it is held, doing nothing otherwise.
        """
        if self._handle is None:
            return
        try:
            self._unlock_handle(self._handle)
        except OSError as e:
            log.debug("Error unlocking %s: %s", self._path, e)
        finally:
            self._handle.close()
            self._handle = None

    @staticmethod
    def _lock_handle(handle: IO[bytes]) -> None:
        """
        Places a non-blocking exclusive lock on the given open file handle.

        :raises OSError: if the lock is held by another process.
        """
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock_handle(handle: IO[bytes]) -> None:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class JdtlsWorkspace:
    """
    Owns the pair of directories a JDTLS process needs exclusively for itself: the Eclipse
    workspace passed via ``-data`` and the OSGi configuration area passed via ``-configuration``.

    Both directories hold mutable state that Eclipse assumes it owns alone. Two JDTLS processes
    sharing them corrupt each other's OSGi bundle cache and workspace metadata, so a workspace
    is claimed through an exclusive OS-level lock. When the preferred (project-specific,
    index-preserving) workspace is already claimed by another Serena process, an ephemeral
    private workspace is used instead, which is deleted again on release.

    Holding the lock also establishes that any Eclipse state found on disk is leftover from a
    process that is no longer running, which is what makes :meth:`discard_unsaved_state` and
    :meth:`reset` safe to apply on startup.

    Instances are created via :meth:`acquire` and must be closed via :meth:`release`.
    """

    _LOCK_FILENAME = ".serena-jdtls.lock"
    _PRIVATE_DIR_MARKER = "-private-"
    _RESOURCES_STATE_DIR = PurePath(".metadata", ".plugins", "org.eclipse.core.resources")

    def __init__(self, root: Path, lock: _ExclusiveFileLock, is_private: bool) -> None:
        """
        Not to be called directly; use :meth:`acquire`.
        """
        self._root = root
        self._lock = lock
        self._is_private = is_private

    @classmethod
    def acquire(cls, workspaces_root: str, workspace_key: str, readonly_config_path: str) -> "JdtlsWorkspace":
        """
        Claims a workspace for exclusive use by the calling process.

        :param workspaces_root: directory under which all JDTLS workspaces are kept.
        :param workspace_key: identifier of the preferred workspace, derived from the project and
            the JDTLS installation; workspaces with the same key share their (warm) Eclipse index.
        :param readonly_config_path: the pristine, read-only OSGi configuration shipped with JDTLS,
            which is copied into the workspace to give the process a writable configuration area.
        :return: the acquired workspace, exclusively owned by this process.
        """
        # remove workspaces left behind by processes that died before releasing them...
        cls._remove_orphaned_private_workspaces(workspaces_root)

        # claim the preferred, index-preserving workspace if it is free, else an ephemeral private one...
        shared_root = Path(workspaces_root, workspace_key)
        lock = cls._try_lock_directory(shared_root)
        is_private = lock is None
        if lock is None:
            private_root = Path(workspaces_root, f"{workspace_key}{cls._PRIVATE_DIR_MARKER}{os.getpid()}-{uuid.uuid4().hex[:8]}")
            lock = cls._try_lock_directory(private_root)
            if lock is None:
                raise OSError(f"Failed to claim a private JDTLS workspace at {private_root}")
            log.info(
                "JDTLS workspace %s is in use by another process; using an isolated workspace at %s",
                shared_root,
                private_root,
            )
            root = private_root
        else:
            root = shared_root

        workspace = cls(root, lock, is_private)
        workspace._prepare(readonly_config_path)
        return workspace

    @property
    def data_dir(self) -> str:
        """
        The Eclipse workspace directory to pass to JDTLS via ``-data``.
        """
        return str(self._root / "data_dir")

    @property
    def config_path(self) -> str:
        """
        The writable OSGi configuration directory to pass to JDTLS via ``-configuration``.
        """
        return str(self._root / "config_path")

    @property
    def is_private(self) -> bool:
        """
        Whether this is an ephemeral workspace that is discarded on release, which is the case
        when the preferred workspace was already claimed by another process.
        """
        return self._is_private

    def discard_unsaved_state(self) -> None:
        """
        Deletes the resource-tree snapshots that Eclipse writes to record changes not yet folded
        into the saved workspace tree.

        A JDTLS process that is terminated rather than shut down gracefully leaves these snapshots
        behind. On the next startup Eclipse replays them against the current state of the file
        system, and any resource that has since been deleted (build output removed by a Maven or
        Gradle run, for example) aborts the replay, which fails the activation of the workspace
        bundle and with it the entire language server -- permanently, since the offending snapshot
        is never cleared. Discarding them costs at most a re-scan of changed resources.

        Only safe while the workspace lock is held, which guarantees that no live process owns the
        snapshots.
        """
        state_dir = Path(self.data_dir) / self._RESOURCES_STATE_DIR
        snapshots = sorted(state_dir.glob("*.snap")) if state_dir.is_dir() else []
        for snapshot in snapshots:
            try:
                snapshot.unlink()
                log.info("Discarded stale Eclipse workspace snapshot %s", snapshot)
            except OSError as e:
                log.warning("Failed to discard stale Eclipse workspace snapshot %s: %s", snapshot, e)

    def reset(self, readonly_config_path: str) -> None:
        """
        Discards all Eclipse state in the workspace, returning it to the state of a workspace that
        has never been used, at the cost of a full re-import of the project on the next startup.

        This is the recovery of last resort for corruption that :meth:`discard_unsaved_state` does
        not address, such as a damaged workspace tree or OSGi bundle cache.

        :param readonly_config_path: the pristine OSGi configuration to re-copy into the workspace.
        """
        log.warning("Resetting JDTLS workspace %s", self._root)
        for path in (Path(self.data_dir) / ".metadata", Path(self.config_path)):
            shutil.rmtree(path, ignore_errors=True)
        self._prepare(readonly_config_path)

    def release(self) -> None:
        """
        Relinquishes ownership of the workspace, deleting it if it is private.

        Never raises; failures to clean up are logged.
        """
        if self._is_private:
            try:
                shutil.rmtree(self._root, ignore_errors=True)
            except Exception as e:
                log.warning("Failed to remove private JDTLS workspace %s: %s", self._root, e)
        self._lock.release()

    def _prepare(self, readonly_config_path: str) -> None:
        """
        Creates the workspace directories and populates the OSGi configuration area from the
        pristine configuration if it is not already populated.
        """
        os.makedirs(self.data_dir, exist_ok=True)
        if not os.path.exists(self.config_path):
            shutil.copytree(readonly_config_path, self.config_path)

    @classmethod
    def _try_lock_directory(cls, root: Path) -> _ExclusiveFileLock | None:
        """
        Creates the given directory if necessary and attempts to claim it.

        :return: the acquired lock, or None if the directory is claimed by another process.
        """
        os.makedirs(root, exist_ok=True)
        lock = _ExclusiveFileLock(str(root / cls._LOCK_FILENAME))
        return lock if lock.try_acquire() else None

    @classmethod
    def _remove_orphaned_private_workspaces(cls, workspaces_root: str) -> None:
        """
        Deletes private workspaces whose owning process is gone, identified by the fact that their
        lock can be acquired.
        """
        root = Path(workspaces_root)
        if not root.is_dir():
            return
        for candidate in root.glob(f"*{cls._PRIVATE_DIR_MARKER}*"):
            if not candidate.is_dir():
                continue
            lock = _ExclusiveFileLock(str(candidate / cls._LOCK_FILENAME))
            if not lock.try_acquire():
                continue
            try:
                shutil.rmtree(candidate, ignore_errors=True)
                log.info("Removed orphaned private JDTLS workspace %s", candidate)
            finally:
                lock.release()
