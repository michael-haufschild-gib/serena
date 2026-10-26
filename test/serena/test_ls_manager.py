"""
Tests for the language server lifecycle guarantees of :class:`LanguageServerManager`: language servers are only
started when needed and are stopped again when they have not been used for a while.
"""

from pathlib import Path
from typing import cast

import pytest

from serena.ls_manager import LanguageServerFactory, LanguageServerManager
from serena.project import Project
from solidlsp import SolidLanguageServer
from solidlsp.ls_config import LanguageServerId


class _FakeLanguageServer:
    def __init__(self, ls_id: LanguageServerId) -> None:
        self.ls_id = ls_id
        self.running = False
        self.caches_closed = False

    def start(self) -> None:
        self.running = True

    def is_running(self) -> bool:
        return self.running

    def stop(self, shutdown_timeout: float = 2.0) -> None:
        self.running = False

    def close_caches(self) -> None:
        self.caches_closed = True


class _FakeFactory:
    def __init__(self) -> None:
        self.created: list[_FakeLanguageServer] = []

    def create_language_server(self, ls_id: LanguageServerId) -> SolidLanguageServer:
        ls = _FakeLanguageServer(ls_id)
        self.created.append(ls)
        return cast(SolidLanguageServer, ls)

    def running(self) -> list[LanguageServerId]:
        return [ls.ls_id for ls in self.created if ls.running]


class _FakeProject:
    def __init__(self, root: Path) -> None:
        self.project_root = str(root)

    def gather_source_files(self) -> list[str]:
        return []


@pytest.fixture
def factory() -> _FakeFactory:
    return _FakeFactory()


def _manager(factory: _FakeFactory, tmp_path: Path, *ls_ids: LanguageServerId) -> LanguageServerManager:
    return LanguageServerManager(list(ls_ids), cast(LanguageServerFactory, factory), cast(Project, _FakeProject(tmp_path)))


class TestLanguageServerManager:
    def test_no_language_server_is_started_before_it_is_needed(self, factory: _FakeFactory, tmp_path: Path) -> None:
        manager = _manager(factory, tmp_path, LanguageServerId.PYTHON, LanguageServerId.TYPESCRIPT)

        assert factory.running() == []
        assert manager.get_active_language_server_ids() == [LanguageServerId.PYTHON, LanguageServerId.TYPESCRIPT]
        assert manager.get_running_language_server_ids() == []

    def test_only_the_responsible_language_server_is_started(self, factory: _FakeFactory, tmp_path: Path) -> None:
        manager = _manager(factory, tmp_path, LanguageServerId.PYTHON, LanguageServerId.TYPESCRIPT)

        ls = manager.get_language_server("src/app.ts")

        assert ls.ls_id == LanguageServerId.TYPESCRIPT
        assert factory.running() == [LanguageServerId.TYPESCRIPT]

    def test_a_running_language_server_is_reused(self, factory: _FakeFactory, tmp_path: Path) -> None:
        manager = _manager(factory, tmp_path, LanguageServerId.PYTHON)

        first = manager.get_language_server("a.py")
        second = manager.get_language_server("b.py")

        assert first is second
        assert len(factory.created) == 1

    def test_files_no_language_server_is_responsible_for_go_to_the_default(self, factory: _FakeFactory, tmp_path: Path) -> None:
        manager = _manager(factory, tmp_path, LanguageServerId.PYTHON, LanguageServerId.TYPESCRIPT)

        assert manager.get_language_server("README.md").ls_id == LanguageServerId.PYTHON
        assert not manager.has_suitable_ls_for_file("README.md")
        assert manager.has_suitable_ls_for_file("main.py")

    def test_idle_language_servers_are_stopped_and_restarted_on_next_use(self, factory: _FakeFactory, tmp_path: Path) -> None:
        manager = _manager(factory, tmp_path, LanguageServerId.PYTHON)
        first = manager.get_language_server("a.py")

        stopped = manager.stop_idle_language_servers(idle_timeout=0)

        assert stopped == [LanguageServerId.PYTHON]
        assert factory.running() == []
        assert cast(_FakeLanguageServer, first).caches_closed
        second = manager.get_language_server("a.py")
        assert second is not first
        assert factory.running() == [LanguageServerId.PYTHON]

    def test_recently_used_language_servers_keep_running(self, factory: _FakeFactory, tmp_path: Path) -> None:
        manager = _manager(factory, tmp_path, LanguageServerId.PYTHON)
        manager.get_language_server("a.py")

        assert manager.stop_idle_language_servers(idle_timeout=3600) == []
        assert factory.running() == [LanguageServerId.PYTHON]

    def test_a_crashed_language_server_is_replaced(self, factory: _FakeFactory, tmp_path: Path) -> None:
        manager = _manager(factory, tmp_path, LanguageServerId.PYTHON)
        crashed = cast(_FakeLanguageServer, manager.get_language_server("a.py"))
        crashed.running = False

        replacement = manager.get_language_server("a.py")

        assert replacement is not crashed
        assert replacement.is_running()

    def test_stop_all_stops_running_language_servers_without_starting_others(self, factory: _FakeFactory, tmp_path: Path) -> None:
        manager = _manager(factory, tmp_path, LanguageServerId.PYTHON, LanguageServerId.TYPESCRIPT)
        manager.get_language_server("a.py")

        manager.stop_all()

        assert factory.running() == []
        assert [ls.ls_id for ls in factory.created] == [LanguageServerId.PYTHON]

    def test_file_system_sync_reports_nothing_while_no_language_server_runs(self, factory: _FakeFactory, tmp_path: Path) -> None:
        manager = _manager(factory, tmp_path, LanguageServerId.PYTHON)

        assert manager.sync_file_system_changes() == 0
        assert factory.running() == []
