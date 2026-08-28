import logging
import os
import tempfile
from typing import Any, Optional

from sensai.util.pickle import dump_pickle, load_pickle

log = logging.getLogger(__name__)


def load_cache(path: str, version: Any) -> Optional[Any]:
    data = load_pickle(path)
    if not isinstance(data, dict) or "__cache_version" not in data:
        log.info("Cache is outdated (expected version %s). Ignoring cache at %s", version, path)
        return None
    saved_version = data["__cache_version"]
    if saved_version != version:
        log.info("Cache is outdated (expected version %s, got %s). Ignoring cache at %s", version, saved_version, path)
        return None
    return data["obj"]


def save_cache(path: str, version: Any, obj: Any) -> None:
    """
    Writes the cache atomically, such that concurrent writers (e.g. several agents working on the
    same project) can never leave a partially written file behind for a subsequent reader.
    """
    data = {"__cache_version": version, "obj": obj}
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)

    # write to a unique temporary file in the target directory and move it into place, which is
    # atomic on the same file system...
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=os.path.basename(path) + ".", suffix=".tmp")
    os.close(fd)
    try:
        dump_pickle(data, tmp_path)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
