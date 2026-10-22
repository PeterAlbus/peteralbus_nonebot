from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
from pathlib import Path
from typing import Any

from .config_store import ConfigValidationError


class ModelRoutesStore:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _read_document(self) -> dict[str, Any]:
        if self.path is None:
            raise OSError("OPS_MODEL_ROUTES_FILE is not configured")
        if self.path.is_symlink():
            raise OSError("model routes file must not be a symlink")
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise OSError("invalid model routes file") from exc
        if (
            not isinstance(document, dict)
            or not isinstance(document.get("providers"), dict)
            or not isinstance(document.get("models"), dict)
        ):
            raise OSError("model routes file must contain providers and models objects")
        return document

    def read_models(self) -> dict[str, Any]:
        with self._lock:
            return {"models": self._read_document()["models"]}

    def replace_models(self, payload: dict[str, Any]) -> dict[str, Any]:
        if set(payload) != {"models"} or not isinstance(payload["models"], dict):
            raise ConfigValidationError("provide only a models object")
        with self._lock:
            document = self._read_document()
            path = self.path
            models = payload["models"]
            for name, model in models.items():
                if not name.strip() or not isinstance(model, dict):
                    raise ConfigValidationError(
                        "each model requires a nonempty name and an object"
                    )
                if set(model) - {
                    "provider",
                    "supports_image_understanding",
                    "create_params",
                }:
                    raise ConfigValidationError(f"unsupported fields for model {name}")
                provider = model.get("provider")
                if not isinstance(provider, str) or not isinstance(
                    document["providers"].get(provider), dict
                ):
                    raise ConfigValidationError(f"unknown provider for model {name}")
                if "supports_image_understanding" in model and not isinstance(
                    model["supports_image_understanding"], bool
                ):
                    raise ConfigValidationError(
                        f"supports_image_understanding must be boolean for {name}"
                    )
                if "create_params" in model and not isinstance(
                    model["create_params"], dict
                ):
                    raise ConfigValidationError(
                        f"create_params must be an object for {name}"
                    )
            document["models"] = models
            try:
                serialized = (
                    json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False)
                    + "\n"
                )
            except ValueError as exc:
                raise ConfigValidationError(
                    "model routes must contain finite JSON values"
                ) from exc
            mode = stat.S_IMODE(path.stat().st_mode)
            fd, temp_name = tempfile.mkstemp(
                prefix=".ops-model-routes-", dir=path.parent
            )
            temp_path = Path(temp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    os.fchmod(output.fileno(), mode)
                    output.write(serialized)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temp_path, path)
            finally:
                temp_path.unlink(missing_ok=True)
            return {"models": models, "restart_required": True}
