from __future__ import annotations

import os
import re
import shlex
import signal
import socket
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from .settings import OpsSettings
from .system import get_system_status


class OpsActionError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_LOG_TIME = re.compile(
    r"(?P<month>\d{2})-(?P<day>\d{2}) "
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
)


def _run(args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise OpsActionError(f"command timed out: {args[0]}", 504) from exc
    except OSError as exc:
        raise OpsActionError(f"command unavailable: {args[0]}", 503) from exc


def _port_listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _parse_log_time(line: str) -> datetime | None:
    match = _LOG_TIME.search(line)
    if not match:
        return None
    now = datetime.now().astimezone()
    try:
        observed = now.replace(
            month=int(match.group("month")),
            day=int(match.group("day")),
            hour=int(match.group("hour")),
            minute=int(match.group("minute")),
            second=int(match.group("second")),
            microsecond=0,
        )
    except ValueError:
        return None
    if observed > now + timedelta(days=1):
        observed = observed.replace(year=observed.year - 1)
    return observed


def _tail_lines(path: Path, max_bytes: int = 4 * 1024 * 1024) -> list[str]:
    try:
        with path.open("rb") as file:
            file.seek(0, os.SEEK_END)
            size = file.tell()
            file.seek(max(0, size - max_bytes))
            return file.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _nonebot_connection(log_path: Path, account: str) -> dict[str, Any]:
    if not account:
        return {"connected": None, "last_event_at": None}
    latest: tuple[datetime | None, bool] | None = None
    for line in _tail_lines(log_path):
        clean = _ANSI.sub("", line)
        if "Bot " not in clean or (account and account not in clean):
            continue
        connected = "connected" in clean and "closed" not in clean
        closed = "closed by peer" in clean or "WebSocket closed" in clean
        if connected or closed:
            latest = (_parse_log_time(clean), connected)
    if latest is None:
        return {"connected": None, "last_event_at": None}
    return {
        "connected": latest[1],
        "last_event_at": latest[0].isoformat() if latest[0] else None,
    }


def _napcat_login_state(log_path: Path) -> dict[str, Any]:
    latest: tuple[datetime | None, str] | None = None
    for line in _tail_lines(log_path):
        clean = _ANSI.sub("", line)
        state: str | None = None
        if "账号状态变更为离线" in clean or "Login Error" in clean:
            state = "offline"
        elif (
            "账号状态变更为在线" in clean
            or "接收 <-" in clean
            or "发送 ->" in clean
        ):
            state = "online"
        if state:
            latest = (_parse_log_time(clean), state)
    if latest is None:
        return {"state": "unknown", "observed_at": None}
    observed_at, state = latest
    return {
        "state": state,
        "observed_at": observed_at.isoformat() if observed_at else None,
    }


class OpsSupervisor:
    def __init__(self, settings: OpsSettings) -> None:
        self.settings = settings
        self._nonebot_restart_lock = threading.Lock()
        self._napcat_restart_lock = threading.Lock()

    def get_system_status(self) -> dict[str, Any]:
        return get_system_status(self.settings.repo_root)

    def _nonebot_env(self) -> dict[str, str | None]:
        return dotenv_values(self.settings.nonebot_env_file, interpolate=False)

    def _nonebot_port(self) -> int:
        raw = self._nonebot_env().get("PORT")
        try:
            port = int(raw or 2333)
        except ValueError:
            return 2333
        return port if 1 <= port <= 65535 else 2333

    def _launchctl_status(self) -> dict[str, Any]:
        target = f"gui/{os.getuid()}/{self.settings.nonebot_label}"
        result = _run(
            ["launchctl", "print", target],
            timeout=self.settings.command_timeout_seconds,
        )
        if result.returncode != 0:
            return {"state": "not_loaded", "pid": None, "last_exit_code": None}
        state_match = re.search(r"^\s*state = ([^\s]+)", result.stdout, re.MULTILINE)
        pid_match = re.search(r"^\s*pid = (\d+)", result.stdout, re.MULTILINE)
        exit_match = re.search(
            r"^\s*last exit code = (-?\d+)", result.stdout, re.MULTILINE
        )
        return {
            "state": state_match.group(1) if state_match else "unknown",
            "pid": int(pid_match.group(1)) if pid_match else None,
            "last_exit_code": int(exit_match.group(1)) if exit_match else None,
        }

    def get_nonebot_status(self) -> dict[str, Any]:
        service = self._launchctl_status()
        env = self._nonebot_env()
        host = (env.get("HOST") or "127.0.0.1").strip().strip("\"'")
        if host in {"", "localhost"}:
            host = "127.0.0.1"
        port = self._nonebot_port()
        listening = _port_listening(host, port)
        connection = _nonebot_connection(
            self.settings.nonebot_log_file, self.settings.napcat_account
        )
        return {
            "launch_agent": self.settings.nonebot_label,
            "service": service,
            "http": {"host": host, "port": port, "listening": listening},
            "onebot": connection,
        }

    def _qq_processes(self) -> tuple[list[int], list[str]]:
        result = _run(["ps", "-axo", "pid=,command="], timeout=5.0)
        if result.returncode != 0:
            raise OpsActionError("could not inspect QQ process table", 503)
        target_path = str(self.settings.napcat_qq_binary)
        account = self.settings.napcat_account
        matching: list[int] = []
        conflicting: list[str] = []
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2 or not parts[1].startswith(target_path):
                continue
            try:
                argv = shlex.split(parts[1])
                pid = int(parts[0])
            except (ValueError, shlex.Error):
                continue
            if not argv or argv[0] != target_path:
                continue
            quick_account = None
            for index, arg in enumerate(argv[:-1]):
                if arg == "-q":
                    quick_account = argv[index + 1]
                    break
            if account and quick_account == account:
                matching.append(pid)
            else:
                conflicting.append(f"pid={pid}")
        return matching, conflicting

    def get_napcat_status(self) -> dict[str, Any]:
        try:
            pids, conflicts = self._qq_processes()
            process_error = None
        except OpsActionError as exc:
            pids, conflicts = [], []
            process_error = str(exc)
        bot_status = self.get_nonebot_status()
        login = _napcat_login_state(self.settings.napcat_stdout_log)
        if bot_status["onebot"]["connected"] is True and pids:
            login = {
                "state": "online",
                "observed_at": bot_status["onebot"]["last_event_at"],
                "source": "NoneBot OneBot connection",
            }
        return {
            "process": {
                "running": bool(pids),
                "pids": pids,
                "account": self.settings.napcat_account or None,
                "conflicting_qq_processes": conflicts,
                "error": process_error,
            },
            "login": login,
            "webui": {
                "host": "127.0.0.1",
                "port": self.settings.napcat_webui_port,
                "listening": _port_listening(
                    "127.0.0.1", self.settings.napcat_webui_port
                ),
            },
            "http_api": {
                "host": "127.0.0.1",
                "port": self.settings.napcat_http_port,
                "listening": _port_listening(
                    "127.0.0.1", self.settings.napcat_http_port
                ),
            },
            "onebot_connected": bot_status["onebot"]["connected"],
        }

    def restart_nonebot(self) -> dict[str, Any]:
        if not self._nonebot_restart_lock.acquire(blocking=False):
            raise OpsActionError("a NoneBot restart is already in progress", 409)
        try:
            return self._restart_nonebot()
        finally:
            self._nonebot_restart_lock.release()

    def _restart_nonebot(self) -> dict[str, Any]:
        target = f"gui/{os.getuid()}/{self.settings.nonebot_label}"
        result = _run(
            ["launchctl", "kickstart", "-k", target],
            timeout=self.settings.command_timeout_seconds,
        )
        if result.returncode != 0:
            raise OpsActionError("launchctl could not restart the NoneBot agent", 502)

        deadline = time.monotonic() + self.settings.startup_timeout_seconds
        status = self.get_nonebot_status()
        while time.monotonic() < deadline:
            status = self.get_nonebot_status()
            if status["service"]["state"] == "running" and status["http"]["listening"]:
                return {"restarted": True, "ready": True, "status": status}
            time.sleep(0.4)
        return {"restarted": True, "ready": False, "status": status}

    def restart_napcat(self) -> dict[str, Any]:
        if not self._napcat_restart_lock.acquire(blocking=False):
            raise OpsActionError("a NapCat restart is already in progress", 409)
        try:
            return self._restart_napcat()
        finally:
            self._napcat_restart_lock.release()

    def _restart_napcat(self) -> dict[str, Any]:
        account = self.settings.napcat_account
        if not account:
            raise OpsActionError("OPS_NAPCAT_ACCOUNT is not configured", 503)
        if not self.settings.napcat_qq_app.is_dir():
            raise OpsActionError("configured QQ app bundle does not exist", 503)
        if not self.settings.napcat_qq_binary.is_file():
            raise OpsActionError("configured QQ executable does not exist", 503)

        pids, conflicts = self._qq_processes()
        if conflicts:
            raise OpsActionError(
                "a QQ process for another or unknown account is running; refusing to stop it",
                409,
            )
        stopped_pids = set(pids)
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                raise OpsActionError("permission denied stopping the NapCat QQ process", 403) from exc

        deadline = time.monotonic() + self.settings.shutdown_timeout_seconds
        while pids and time.monotonic() < deadline:
            current, _ = self._qq_processes()
            if not any(pid in current for pid in pids):
                break
            time.sleep(0.25)
        else:
            if pids:
                current, _ = self._qq_processes()
                if any(pid in current for pid in pids):
                    raise OpsActionError("QQ did not stop before the restart timeout", 504)

        launch = _run(
            [
                "open",
                "-a",
                str(self.settings.napcat_qq_app),
                "--args",
                "--no-sandbox",
                "-q",
                account,
            ],
            timeout=self.settings.command_timeout_seconds,
        )
        if launch.returncode != 0:
            raise OpsActionError("macOS could not launch QQ/NapCat", 503)

        deadline = time.monotonic() + self.settings.startup_timeout_seconds
        status = self.get_napcat_status()
        while time.monotonic() < deadline:
            status = self.get_napcat_status()
            ports_ready = status["webui"]["listening"] and status["http_api"]["listening"]
            has_new_process = any(
                pid not in stopped_pids for pid in status["process"]["pids"]
            )
            if has_new_process and ports_ready:
                return {
                    "restarted": True,
                    "ready": True,
                    "status": status,
                }
            time.sleep(0.5)
        return {
            "restarted": True,
            "ready": False,
            "status": status,
        }
