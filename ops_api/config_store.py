from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from dotenv import dotenv_values, set_key

_KEY_LINE = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=")
_SECRET_KEY = re.compile(r"(?:API_KEY|ACCESS_TOKEN|PASSWORD)$", re.IGNORECASE)
_EXTRA_KEYS = {"LLM_CHAT_IMAGE_UNDERSTANDING", "LLM_CHAT_ROUTES_FILE"}
_TYPE_OVERRIDES = {"LLM_CHAT_IMAGE_UNDERSTANDING": bool, "LLM_CHAT_ROUTES_FILE": str}
_LOCK = threading.Lock()


def _template_keys(template_path: Path) -> set[str]:
    keys: set[str] = set()
    if template_path.is_file():
        for line in template_path.read_text(encoding="utf-8").splitlines():
            match = _KEY_LINE.match(line)
            if match:
                keys.add(match.group(1))
            else:
                optional = re.match(r"^\s*#\s*([A-Z][A-Z0-9_]*)\s*=", line)
                if optional:
                    keys.add(optional.group(1))
    return keys | _EXTRA_KEYS


def _expected_type(key: str, template_values: Mapping[str, Any]) -> type:
    if key in _TYPE_OVERRIDES:
        return _TYPE_OVERRIDES[key]
    raw = template_values.get(key)
    if raw is None or raw == "":
        return str
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return str
    if isinstance(parsed, bool):
        return bool
    if isinstance(parsed, int):
        return int
    if isinstance(parsed, float):
        return float
    if isinstance(parsed, (list, dict)):
        return type(parsed)
    return str


def _parse_config_value(raw: str | None) -> Any:
    if raw is None:
        return ""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


class ConfigValidationError(ValueError):
    pass


class EnvConfigStore:
    """Read and safely patch only keys documented by the repository template."""

    def __init__(self, env_path: Path, template_path: Path) -> None:
        self.env_path = env_path.expanduser()
        self.template_path = template_path.expanduser()
        self.allowed_keys = _template_keys(self.template_path)

    @staticmethod
    def _is_secret(key: str) -> bool:
        return bool(_SECRET_KEY.search(key))

    def read(self) -> dict[str, Any]:
        if not self.env_path.is_file():
            raise FileNotFoundError("NoneBot .env file not found")
        current = dotenv_values(self.env_path, interpolate=False)
        config: dict[str, Any] = {}
        redacted: list[str] = []
        for key in sorted(self.allowed_keys):
            value = current.get(key)
            if self._is_secret(key):
                config[key] = {"configured": bool(value), "redacted": True}
                redacted.append(key)
            else:
                config[key] = _parse_config_value(value)
        return {"config": config, "redacted_fields": redacted}

    def update(self, values: Mapping[str, Any]) -> dict[str, Any]:
        if not values:
            raise ConfigValidationError("provide at least one configuration field")
        unknown = sorted(set(values) - self.allowed_keys)
        if unknown:
            raise ConfigValidationError(
                "unsupported configuration fields: " + ", ".join(unknown)
            )

        defaults = dotenv_values(self.template_path, interpolate=False)
        normalized: dict[str, str] = {}
        for key, value in values.items():
            if value is None and self._is_secret(key):
                value = ""
            expected = _expected_type(key, defaults)
            if not self._valid_type(value, expected):
                raise ConfigValidationError(
                    f"{key} must be a {self._type_name(expected)}"
                )
            if key == "HOST" and value not in {"127.0.0.1", "::1", "localhost"}:
                raise ConfigValidationError("HOST must remain loopback-only")
            if isinstance(value, str) and ("\x00" in value or "\n" in value or "\r" in value):
                raise ConfigValidationError(f"{key} cannot contain newlines or NUL")
            serialized = (
                json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                if not isinstance(value, str)
                else value
            )
            if "\x00" in serialized or "\n" in serialized or "\r" in serialized:
                raise ConfigValidationError(f"{key} cannot contain newlines or NUL")
            normalized[key] = serialized

        with _LOCK:
            self._atomic_update(normalized)
        return self.read()

    @staticmethod
    def _type_name(expected: type) -> str:
        return {
            str: "string",
            int: "integer",
            float: "number",
            bool: "boolean",
            list: "array",
            dict: "object",
        }.get(expected, "value")

    @staticmethod
    def _valid_type(value: Any, expected: type) -> bool:
        if expected is str:
            return isinstance(value, str)
        if expected is int:
            return isinstance(value, int) and not isinstance(value, bool)
        if expected is float:
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if expected is bool:
            return isinstance(value, bool)
        if expected is list:
            return isinstance(value, list)
        if expected is dict:
            return isinstance(value, dict)
        return False

    def _atomic_update(self, updates: Mapping[str, str]) -> None:
        path = self.env_path
        if path.is_symlink():
            raise ConfigValidationError("refusing to edit a symlinked .env file")
        if not path.is_file():
            raise FileNotFoundError(f"NoneBot env file not found: {path}")

        original = path.read_text(encoding="utf-8")
        # The file contains API credentials; keep it readable and writable only
        # by the account running the bot and this local management service.
        mode = stat.S_IMODE(path.stat().st_mode)
        mode = (mode & 0o600) | 0o600
        lines = original.splitlines(keepends=True)
        for key in updates:
            count = sum(1 for line in lines if (m := _KEY_LINE.match(line)) and m.group(1) == key)
            if count > 1:
                raise ConfigValidationError(
                    f"duplicate .env entries for {key}; resolve duplicates manually"
                )

        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".ops-env-", dir=path.parent)
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            temp_path.write_text(original, encoding="utf-8")
            os.chmod(temp_path, mode)
            for key, value in updates.items():
                result = set_key(
                    str(temp_path),
                    key,
                    value,
                    quote_mode="always",
                    export=False,
                )
                success = result[0]
                if not success:
                    raise OSError(f"failed to update {key}")
            os.chmod(temp_path, mode)
            with temp_path.open("rb") as updated_file:
                os.fsync(updated_file.fileno())
            os.replace(temp_path, path)
            os.chmod(path, mode)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
