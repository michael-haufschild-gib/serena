from types import SimpleNamespace

from click.testing import CliRunner

from serena.cli import TopLevelCommands
from serena.util.shell import ShellCommandResult

CODEX_ADD_COMMAND = "codex mcp add serena -- serena start-mcp-server --context=codex --project-from-cwd"


def _result(command: str, return_code: int = 0, stdout: str = "", stderr: str = "") -> ShellCommandResult:
    return ShellCommandResult(stdout=stdout, stderr=stderr, return_code=return_code, cwd=".")


def test_setup_codex_success(monkeypatch):
    commands: list[str] = []

    def fake_execute_shell_command(command: str, capture_stderr: bool = False) -> ShellCommandResult:
        commands.append(command)
        if command == "codex --version":
            return _result(command, stdout="codex-cli 0.40.0\n")
        if command == CODEX_ADD_COMMAND:
            return _result(command)
        return _result(command, return_code=1)

    monkeypatch.setattr("serena.config.client_setup.execute_shell_command", fake_execute_shell_command)

    result = CliRunner().invoke(TopLevelCommands.setup, ["codex"])

    assert result.exit_code == 0, result.output
    assert "successfully set up for codex" in result.output
    assert commands == ["codex --version", CODEX_ADD_COMMAND]


def test_setup_codex_not_applicable_exits_1(monkeypatch):
    def fake_execute_shell_command(command: str, capture_stderr: bool = False) -> ShellCommandResult:
        if command == "codex --version":
            return _result(command, return_code=1)
        return _result(command)

    monkeypatch.setattr("serena.config.client_setup.execute_shell_command", fake_execute_shell_command)

    result = CliRunner().invoke(TopLevelCommands.setup, ["codex"])

    assert result.exit_code == 1
    assert "Cannot apply setup for client 'codex'" in result.output


def test_setup_codex_apply_failure_exits_1(monkeypatch):
    def fake_execute_shell_command(command: str, capture_stderr: bool = False) -> ShellCommandResult:
        if command == "codex --version":
            return _result(command, stdout="codex-cli 0.40.0\n")
        if command == CODEX_ADD_COMMAND:
            return _result(command, return_code=1, stderr="boom")
        return _result(command, return_code=1)

    monkeypatch.setattr("serena.config.client_setup.execute_shell_command", fake_execute_shell_command)

    result = CliRunner().invoke(TopLevelCommands.setup, ["codex"])

    assert result.exit_code == 1
    assert "Failed to set up Serena for codex" in result.output
    assert "boom" in result.output


def test_init_suggests_codex_when_detected(monkeypatch):
    def fake_execute_shell_command(command: str, capture_stderr: bool = False) -> ShellCommandResult:
        if command == "codex --version":
            return _result(command, stdout="codex-cli 0.40.0\n")
        return _result(command, return_code=1)

    def fake_from_config_file(**kwargs) -> SimpleNamespace:
        return SimpleNamespace(config_file_path="/tmp/serena_config.yml")

    monkeypatch.setattr("serena.config.client_setup.execute_shell_command", fake_execute_shell_command)
    monkeypatch.setattr("serena.cli.SerenaConfig.from_config_file", fake_from_config_file)

    result = CliRunner().invoke(TopLevelCommands.init, [])

    assert result.exit_code == 0, result.output
    assert "serena setup codex" in result.output
    assert "serena setup claude-code" not in result.output


def test_init_accepts_removed_language_backend_option(monkeypatch):
    monkeypatch.setattr("serena.config.client_setup.execute_shell_command", lambda command, capture_stderr=False: _result(command, 1))
    monkeypatch.setattr("serena.cli.SerenaConfig.from_config_file", lambda **kwargs: SimpleNamespace(config_file_path="/tmp/x.yml"))

    result = CliRunner().invoke(TopLevelCommands.init, ["--language-backend", "LSP"])

    assert result.exit_code == 0, result.output
