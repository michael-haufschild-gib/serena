"""
Tests for the guarantees of the persistent symbol caches.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from solidlsp.util.cache import PersistentSymbolCache

CACHE_VERSION = 1


def _put_from_other_process(db_path: Path, path: str, value: list[int], kill_afterwards: bool = False) -> None:
    """
    Adds an entry to the cache from a separate process, optionally killing that process right after the put.
    """
    script = (
        "import os, signal, sys\n"
        "from pathlib import Path\n"
        "from solidlsp.util.cache import PersistentSymbolCache\n"
        f"cache = PersistentSymbolCache(Path({str(db_path)!r}), {CACHE_VERSION!r})\n"
        f"cache.put({path!r}, 'hash', {value!r})\n" + ("os.kill(os.getpid(), signal.SIGKILL)\n" if kill_afterwards else "")
    )
    subprocess.run([sys.executable, "-c", script], check=not kill_afterwards, timeout=60)


class TestPersistentSymbolCache:
    def test_entries_are_available_to_a_later_instance(self, tmp_path: Path) -> None:
        db_path = tmp_path / "symbols.sqlite"
        PersistentSymbolCache(db_path, CACHE_VERSION).put("a.py", "h1", [1, 2, 3])

        assert PersistentSymbolCache(db_path, CACHE_VERSION).get("a.py") == ("h1", [1, 2, 3])

    def test_put_replaces_the_previous_entry(self, tmp_path: Path) -> None:
        cache: PersistentSymbolCache[list[int]] = PersistentSymbolCache(tmp_path / "symbols.sqlite", CACHE_VERSION)
        cache.put("a.py", "h1", [1])
        cache.put("a.py", "h2", [2])

        assert cache.get("a.py") == ("h2", [2])
        assert PersistentSymbolCache(tmp_path / "symbols.sqlite", CACHE_VERSION).get("a.py") == ("h2", [2])

    def test_missing_entry(self, tmp_path: Path) -> None:
        assert PersistentSymbolCache(tmp_path / "symbols.sqlite", CACHE_VERSION).get("missing.py") is None

    def test_a_different_version_discards_all_entries(self, tmp_path: Path) -> None:
        db_path = tmp_path / "symbols.sqlite"
        PersistentSymbolCache(db_path, CACHE_VERSION).put("a.py", "h1", [1])

        assert PersistentSymbolCache(db_path, (CACHE_VERSION, "changed")).get("a.py") is None
        assert PersistentSymbolCache(db_path, CACHE_VERSION).get("a.py") is None

    def test_entries_beyond_the_memory_limit_are_still_served(self, tmp_path: Path) -> None:
        cache: PersistentSymbolCache[int] = PersistentSymbolCache(tmp_path / "symbols.sqlite", CACHE_VERSION, max_entries_in_memory=2)
        for i in range(10):
            cache.put(f"f{i}.py", f"h{i}", i)

        assert [cache.get(f"f{i}.py") for i in range(10)] == [(f"h{i}", i) for i in range(10)]

    def test_concurrent_writers_do_not_lose_each_others_entries(self, tmp_path: Path) -> None:
        """
        Several agents may work on one project at once; an entry one of them adds must survive
        the other one adding (and persisting) its own entries.
        """
        db_path = tmp_path / "symbols.sqlite"
        cache: PersistentSymbolCache[list[int]] = PersistentSymbolCache(db_path, CACHE_VERSION)
        cache.put("mine.py", "hash", [1])

        _put_from_other_process(db_path, "theirs.py", [2])
        cache.put("mine2.py", "hash", [3])

        reopened: PersistentSymbolCache[list[int]] = PersistentSymbolCache(db_path, CACHE_VERSION)
        assert reopened.get("mine.py") == ("hash", [1])
        assert reopened.get("theirs.py") == ("hash", [2])
        assert reopened.get("mine2.py") == ("hash", [3])

    def test_entries_survive_a_killed_process(self, tmp_path: Path) -> None:
        db_path = tmp_path / "symbols.sqlite"
        _put_from_other_process(db_path, "a.py", [1], kill_afterwards=True)

        assert PersistentSymbolCache(db_path, CACHE_VERSION).get("a.py") == ("hash", [1])

    def test_a_corrupt_database_degrades_to_memory_only(self, tmp_path: Path) -> None:
        db_path = tmp_path / "symbols.sqlite"
        db_path.write_bytes(b"this is not an sqlite database" * 100)

        cache: PersistentSymbolCache[list[int]] = PersistentSymbolCache(db_path, CACHE_VERSION)
        cache.put("a.py", "h1", [1])

        assert cache.get("a.py") == ("h1", [1])
