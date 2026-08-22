import multi_llm_chat.cli_runner as cli_runner_module
import pytest
from multi_llm_chat.cli_runner import (
    CliCommandRejected,
    DockerCliRunner,
    validate_cli_command,
)


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /workspace/data",
        "echo ok; sudo shutdown now",
        "git reset --hard",
        "python x.py | killall python",
        "echo ok\nrm -rf /workspace/data",
    ],
)
def test_high_risk_commands_are_rejected(command):
    with pytest.raises(CliCommandRejected):
        validate_cli_command(command)


def test_normal_cli_commands_are_allowed():
    validate_cli_command("python -c 'print(6 * 7)'")
    validate_cli_command("jq '.name' input.json")


def test_workspace_removal_cannot_escape_configured_root(tmp_path):
    runner = DockerCliRunner("test", tmp_path / "workspaces", 10, 2000)
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(ValueError):
        runner.remove_workspace(outside)

    assert outside.is_dir()


@pytest.mark.asyncio
async def test_docker_process_uses_the_declared_isolation_boundary(
    tmp_path,
    monkeypatch,
):
    captured = {}

    class Process:
        def __init__(self):
            self.returncode = 0
            self.stdout = cli_runner_module.asyncio.StreamReader()
            self.stderr = cli_runner_module.asyncio.StreamReader()
            self.stdout.feed_data(b"ok\n")
            self.stdout.feed_eof()
            self.stderr.feed_eof()

        async def wait(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

    async def create_subprocess_exec(*arguments, **kwargs):
        captured["arguments"] = arguments
        captured["kwargs"] = kwargs
        return Process()

    monkeypatch.setattr(
        cli_runner_module.asyncio,
        "create_subprocess_exec",
        create_subprocess_exec,
    )
    runner = DockerCliRunner("tool-image", tmp_path / "workspaces", 10, 2000)
    workspace = runner.create_workspace("turn-1")

    result = await runner.run("python -c 'print(42)'", workspace)

    arguments = captured["arguments"]
    assert arguments[:2] == ("docker", "run")
    assert ("--network", "none") == _flag_pair(arguments, "--network")
    assert "--read-only" in arguments
    assert ("--cap-drop", "ALL") == _flag_pair(arguments, "--cap-drop")
    assert ("--user", "65534:65534") == _flag_pair(arguments, "--user")
    mount = _flag_pair(arguments, "--mount")[1]
    assert f"src={workspace.resolve()}" in mount
    assert "dst=/workspace" in mount
    assert result.stdout == "ok\n"
    assert result.exit_code == 0


def _flag_pair(arguments, flag):
    index = arguments.index(flag)
    return arguments[index], arguments[index + 1]
