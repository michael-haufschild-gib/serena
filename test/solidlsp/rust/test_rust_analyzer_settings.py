import pytest

from solidlsp.language_servers.rust_analyzer import RustAnalyzer

pytestmark = pytest.mark.rust


def _make_server() -> RustAnalyzer:
    return object.__new__(RustAnalyzer)


def test_initialization_never_builds_or_checks_code() -> None:
    """rust-analyzer must analyse sources only: no cargo check, no build scripts, no proc-macro compilation."""
    initialization_options = _make_server()._create_base_initialize_params()["initializationOptions"]

    assert initialization_options["cachePriming"]["enable"] is False
    assert initialization_options["checkOnSave"] is False
    assert initialization_options["cargo"]["buildScripts"]["enable"] is False
    assert initialization_options["procMacro"]["enable"] is False
