import logging
import pickle
import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Hashable
from pathlib import Path
from typing import Generic, TypeVar

log = logging.getLogger(__name__)

TValue = TypeVar("TValue")


class PersistentSymbolCache(Generic[TValue]):
    """
    A persistent mapping from relative file paths to ``(content_hash, value)`` pairs, stored in an SQLite
    database and fronted by a small in-memory LRU.

    Entries are read from disk on demand and written as soon as they are added, so

      * memory use is bounded by the LRU size rather than by the size of the project,
      * several processes working on the same project (e.g. agents in different chats) share one
        cache and never overwrite each other's entries, and
      * nothing is lost if the process is killed.

    The cache is scoped to a version: each entry is stored with the version it was written for and is only ever
    returned to readers of that version, so processes running different versions of the code can share the
    database file safely; opening the cache discards the entries of all other versions.
    If the database cannot be used (e.g. it is corrupt or locked for too long), the cache degrades
    to its in-memory part instead of failing the caller.
    """

    _BUSY_TIMEOUT_MS = 5000
    _SCHEMA_VERSION = 2

    def __init__(self, db_path: Path, version: Hashable, max_entries_in_memory: int = 128) -> None:
        """
        :param db_path: the path of the database file; it is created if it does not exist
        :param version: the version of the cached content; entries stored under any other version are discarded
        :param max_entries_in_memory: the number of most recently used entries kept in memory
        """
        self._db_path = db_path
        self._max_entries_in_memory = max_entries_in_memory
        self._memory: OrderedDict[str, tuple[str, TValue]] = OrderedDict()
        self._lock = threading.Lock()
        self._version = repr(version)
        self._connection: sqlite3.Connection | None = None
        try:
            self._connection = self._open(db_path, self._version)
        except sqlite3.Error as e:
            log.warning("Symbol cache %s is unusable (%s); continuing with an in-memory cache only", db_path, e)

    @classmethod
    def _open(cls, db_path: Path, version: str) -> sqlite3.Connection:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(db_path, timeout=cls._BUSY_TIMEOUT_MS / 1000, check_same_thread=False, isolation_level=None)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("BEGIN IMMEDIATE")
            try:
                if connection.execute("PRAGMA user_version").fetchone()[0] != cls._SCHEMA_VERSION:
                    connection.execute("DROP TABLE IF EXISTS entries")
                    connection.execute("DROP TABLE IF EXISTS meta")
                    connection.execute(
                        "CREATE TABLE entries (version TEXT NOT NULL, path TEXT NOT NULL, content_hash TEXT NOT NULL, "
                        "payload BLOB NOT NULL, PRIMARY KEY (version, path))"
                    )
                    connection.execute(f"PRAGMA user_version = {cls._SCHEMA_VERSION}")
                deleted = connection.execute("DELETE FROM entries WHERE version != ?", (version,)).rowcount
                if deleted:
                    log.info("Discarded %d entries of other versions from symbol cache %s", deleted, db_path)
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        except BaseException:
            connection.close()
            raise
        return connection

    def _remember(self, path: str, entry: tuple[str, TValue]) -> None:
        self._memory[path] = entry
        self._memory.move_to_end(path)
        while len(self._memory) > self._max_entries_in_memory:
            self._memory.popitem(last=False)

    def _disable_persistence(self, error: sqlite3.Error) -> None:
        log.warning("Symbol cache %s failed (%s); continuing with an in-memory cache only", self._db_path, error)
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def get(self, path: str) -> tuple[str, TValue] | None:
        """
        :param path: the relative path of the file
        :return: the content hash the value was computed for and the value, or None if there is no entry
        """
        with self._lock:
            entry = self._memory.get(path)
            if entry is not None:
                self._memory.move_to_end(path)
                return entry
            if self._connection is None:
                return None
            try:
                row = self._connection.execute(
                    "SELECT content_hash, payload FROM entries WHERE version = ? AND path = ?", (self._version, path)
                ).fetchone()
            except sqlite3.Error as e:
                self._disable_persistence(e)
                return None
            if row is None:
                return None
            try:
                entry = (row[0], pickle.loads(row[1]))
            except Exception as e:
                log.warning("Discarding unreadable symbol cache entry for %s: %s", path, e)
                return None
            self._remember(path, entry)
            return entry

    def put(self, path: str, content_hash: str, value: TValue) -> None:
        """
        Stores the value computed for the given content of a file, replacing any previous entry.

        :param path: the relative path of the file
        :param content_hash: the hash of the file content the value was computed for
        :param value: the value
        """
        with self._lock:
            self._remember(path, (content_hash, value))
            if self._connection is None:
                return
            try:
                payload = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
            except Exception as e:
                log.warning("Not persisting symbol cache entry for %s, which cannot be pickled: %s", path, e)
                return
            try:
                self._connection.execute(
                    "INSERT OR REPLACE INTO entries (version, path, content_hash, payload) VALUES (?, ?, ?, ?)",
                    (self._version, path, content_hash, payload),
                )
            except sqlite3.Error as e:
                self._disable_persistence(e)

    def close(self) -> None:
        """
        Closes the database connection; the cache keeps working in memory only.
        """
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
