import asyncio
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple
from uuid import uuid4

BLOCKED_COMMAND_PATTERN = re.compile(
    r"(?:^|[;&|\n]\s*)(?:sudo\s+)?"
    r"(?:rm|rmdir|shred|shutdown|reboot|halt|poweroff|mkfs(?:\.[a-z0-9]+)?|"
    r"mount|umount|kill|pkill|killall)\b|"
    r"(?:^|[;&|\n]\s*)git\s+(?:clean|reset)\b",
    re.IGNORECASE,
)


class CliCommandRejected(ValueError):
    pass


@dataclass(frozen=True)
class CliExecutionResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    truncated: bool


class DockerCliRunner:
    def __init__(
        self,
        image: str,
        workspace_root: Path,
        timeout_seconds: int,
        output_max_chars: int,
    ) -> None:
        self._image = image
        self._workspace_root = workspace_root
        self._timeout_seconds = max(1, timeout_seconds)
        self._output_max_chars = max(1000, output_max_chars)

    def create_workspace(self, turn_id: str) -> Path:
        self._workspace_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = Path(
            tempfile.mkdtemp(
                prefix=f"turn-{turn_id[:12]}-",
                dir=str(self._workspace_root),
            )
        )
        path.chmod(0o777)
        return path

    def remove_workspace(self, workspace: Path) -> None:
        if workspace.parent.resolve() != self._workspace_root.resolve():
            raise ValueError("工具工作目录不属于配置的根目录")
        shutil.rmtree(workspace)

    async def run(
        self,
        command: str,
        workspace: Path,
        timeout_seconds: Optional[int] = None,
    ) -> CliExecutionResult:
        validate_cli_command(command)
        if workspace.parent.resolve() != self._workspace_root.resolve():
            raise ValueError("工具工作目录不属于配置的根目录")
        timeout = min(
            max(1, timeout_seconds or self._timeout_seconds),
            self._timeout_seconds,
        )
        container_name = f"nonebot-tool-{uuid4().hex[:16]}"
        mount = f"type=bind,src={workspace.resolve()},dst=/workspace,rw"
        process = await asyncio.create_subprocess_exec(
            "docker",
            "run",
            "--rm",
            "--name",
            container_name,
            "--network",
            "none",
            "--memory",
            "256m",
            "--cpus",
            "1",
            "--pids-limit",
            "64",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "65534:65534",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=64m",
            "--mount",
            mount,
            "--workdir",
            "/workspace",
            self._image,
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        stdout_task = asyncio.create_task(
            _read_limited(process.stdout, self._output_max_chars)
        )
        stderr_task = asyncio.create_task(
            _read_limited(process.stderr, self._output_max_chars)
        )
        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
            await _kill_container(container_name)
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        stdout, stdout_truncated = await stdout_task
        stderr, stderr_truncated = await stderr_task
        return CliExecutionResult(
            exit_code=process.returncode if process.returncode is not None else -1,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            truncated=stdout_truncated or stderr_truncated,
        )


def validate_cli_command(command: str) -> None:
    stripped = command.strip()
    if not stripped:
        raise CliCommandRejected("CLI 命令不能为空")
    if len(stripped) > 4000:
        raise CliCommandRejected("CLI 命令过长")
    if BLOCKED_COMMAND_PATTERN.search(stripped):
        raise CliCommandRejected("命令包含被禁止的高危操作")


async def _read_limited(
    stream: Optional[asyncio.StreamReader],
    max_chars: int,
) -> Tuple[str, bool]:
    if stream is None:
        return "", False
    chunks = []
    stored = 0
    total_seen = 0
    truncated = False
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        text = chunk.decode("utf-8", errors="replace")
        total_seen += len(text)
        if stored < max_chars:
            remaining = max_chars - stored
            chunks.append(text[:remaining])
            stored += min(len(text), remaining)
        truncated = total_seen > max_chars
    return "".join(chunks), truncated


async def _kill_container(container_name: str) -> None:
    try:
        killer = await asyncio.create_subprocess_exec(
            "docker",
            "kill",
            container_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(killer.wait(), timeout=5)
    except (FileNotFoundError, asyncio.TimeoutError):
        return
