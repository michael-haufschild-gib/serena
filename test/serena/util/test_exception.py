import pytest

from serena.util.exception import report_fatal_exception


def test_report_fatal_exception_prints_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    report_fatal_exception(ValueError("Test error message"))

    assert "Fatal exception: Test error message" in capsys.readouterr().err
