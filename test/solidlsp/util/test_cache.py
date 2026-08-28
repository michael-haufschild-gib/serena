"""
Tests for the durability guarantees of the symbol caches.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from solidlsp.util.cache import load_cache, save_cache

CACHE_VERSION = 1


class TestCacheDurability:
    def test_roundtrip(self, tmp_path: Path) -> None:
        path = str(tmp_path / "symbols.pkl")
        save_cache(path, CACHE_VERSION, {"a.py": [1, 2, 3]})
        assert load_cache(path, CACHE_VERSION) == {"a.py": [1, 2, 3]}

    def test_a_previously_saved_cache_survives_an_interrupted_save(self, tmp_path: Path) -> None:
        """
        Several agents may work on one project at once, so a save that is cut short -- by a killed
        process, for example -- must never leave a reader with a half-written cache file.
        """
        path = str(tmp_path / "symbols.pkl")
        save_cache(path, CACHE_VERSION, {"a.py": [1, 2, 3]})

        _kill_process_while_saving(path, tmp_path)

        assert load_cache(path, CACHE_VERSION) == {"a.py": [1, 2, 3]}


def _kill_process_while_saving(path: str, tmp_path: Path) -> None:
    """
    Runs a save in a separate process and kills that process part-way through writing.
    """
    started_marker = tmp_path / "save_started"
    script = (
        "import pickle\n"
        "from pathlib import Path\n"
        "import solidlsp.util.cache as cache\n"
        "original = cache.dump_pickle\n"
        "def slow_dump(obj, p):\n"
        "    original(obj, p)\n"
        f"    Path({str(started_marker)!r}).write_text('x')\n"
        "    import time; time.sleep(60)\n"
        "cache.dump_pickle = slow_dump\n"
        f"cache.save_cache({path!r}, {CACHE_VERSION!r}, {{'b.py': [9]}})\n"
    )
    process = subprocess.Popen([sys.executable, "-c", script])
    try:
        for _ in range(600):
            if started_marker.exists():
                break
            process.poll()
            import time

            time.sleep(0.1)
        assert started_marker.exists(), "child process never reached the write"
    finally:
        process.kill()
        process.wait(timeout=30)
