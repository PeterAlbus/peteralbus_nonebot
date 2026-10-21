from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


def _path_value(value: str | None, default: Path, root: Path) -> Path:
    raw = Path(value).expanduser() if value else default
    return raw if raw.is_absolute() else (root / raw)


@dataclass(frozen=True)
class OpsSettings:
    repo_root: Path
    api_token: str = field(repr=False)
    api_host: str = "127.0.0.1"
    api_port: int = 8765
    nonebot_label: str = "com.peteralbus.nonebot"
    nonebot_env_file: Path = Path(".env")
    nonebot_log_file: Path = Path("service_logs/nonebot.stdout.log")
    napcat_account: str = ""
    napcat_qq_app: Path = Path("/Applications/QQ.app")
    napcat_qq_binary: Path = Path("/Applications/QQ.app/Contents/MacOS/QQ")
    napcat_stdout_log: Path = Path("service_logs/napcat.stdout.log")
    napcat_webui_port: int = 6099
    napcat_http_port: int = 3001
    shutdown_timeout_seconds: float = 20.0
    startup_timeout_seconds: float = 35.0
    command_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        root = self.repo_root.expanduser().resolve()
        object.__setattr__(self, "repo_root", root)
        if len(self.api_token) < 32:
            raise ValueError("OPS_API_TOKEN must contain at least 32 characters")
        try:
            address = ipaddress.ip_address(self.api_host)
        except ValueError as exc:
            raise ValueError("OPS_API_HOST must be a loopback IP address") from exc
        if not address.is_loopback:
            raise ValueError("OPS_API_HOST must be a loopback IP address")
        for name in ("api_port", "napcat_webui_port", "napcat_http_port"):
            port = getattr(self, name)
            if not 1 <= port <= 65535:
                raise ValueError(f"{name} must be between 1 and 65535")
        if self.shutdown_timeout_seconds <= 0 or self.startup_timeout_seconds <= 0:
            raise ValueError("restart timeouts must be positive")

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        repo_root: Path | None = None,
    ) -> OpsSettings:
        values = os.environ if environ is None else environ
        root = (repo_root or Path(__file__).resolve().parents[1]).expanduser().resolve()
        token = values.get("OPS_API_TOKEN", "")
        if len(token) < 32:
            raise ValueError(
                "Set OPS_API_TOKEN to a fixed random value of at least 32 characters"
            )

        def int_value(name: str, default: int) -> int:
            try:
                return int(values.get(name, str(default)))
            except ValueError as exc:
                raise ValueError(f"{name} must be an integer") from exc

        def float_value(name: str, default: float) -> float:
            try:
                return float(values.get(name, str(default)))
            except ValueError as exc:
                raise ValueError(f"{name} must be a number") from exc

        return cls(
            repo_root=root,
            api_token=token,
            api_host=values.get("OPS_API_HOST", "127.0.0.1"),
            api_port=int_value("OPS_API_PORT", 8765),
            nonebot_label=values.get("OPS_NONEBOT_LABEL", "com.peteralbus.nonebot"),
            nonebot_env_file=_path_value(
                values.get("OPS_NONEBOT_ENV_FILE"), Path(".env"), root
            ),
            nonebot_log_file=_path_value(
                values.get("OPS_NONEBOT_LOG_FILE"),
                Path.home() / "Documents/napcat/service_logs/nonebot.stdout.log",
                root,
            ),
            napcat_account=values.get("OPS_NAPCAT_ACCOUNT", "").strip(),
            napcat_qq_app=_path_value(
                values.get("OPS_NAPCAT_QQ_APP"),
                Path("/Applications/QQ.app"),
                root,
            ),
            napcat_qq_binary=_path_value(
                values.get("OPS_NAPCAT_QQ_BINARY"),
                Path("/Applications/QQ.app/Contents/MacOS/QQ"),
                root,
            ),
            napcat_stdout_log=_path_value(
                values.get("OPS_NAPCAT_STDOUT_LOG"),
                Path.home() / "Documents/napcat/service_logs/napcat.stdout.log",
                root,
            ),
            napcat_webui_port=int_value("OPS_NAPCAT_WEBUI_PORT", 6099),
            napcat_http_port=int_value("OPS_NAPCAT_HTTP_PORT", 3001),
            shutdown_timeout_seconds=float_value(
                "OPS_SHUTDOWN_TIMEOUT_SECONDS", 20.0
            ),
            startup_timeout_seconds=float_value(
                "OPS_STARTUP_TIMEOUT_SECONDS", 35.0
            ),
            command_timeout_seconds=float_value(
                "OPS_COMMAND_TIMEOUT_SECONDS", 5.0
            ),
        )
