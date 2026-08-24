from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any

LOG_FILE_PREFIX = "llm-requests-"
IMAGE_MIME_TYPES = ("image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp")
_WRITE_LOCK = Lock()


def append_raw_request(
    directory: Path,
    request_id: str,
    request_type: str,
    request_body: dict[str, Any],
    metadata: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> Path:
    return _append_raw_record(
        directory=directory,
        request_id=request_id,
        request_type=request_type,
        kind="request",
        payload_name="request",
        payload=request_body,
        metadata=metadata,
        now=now,
    )


def append_raw_response(
    directory: Path,
    request_id: str,
    request_type: str,
    response_body: Any,
    metadata: dict[str, Any] | None = None,
    handling: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> Path:
    return _append_raw_record(
        directory=directory,
        request_id=request_id,
        request_type=request_type,
        kind="response",
        payload_name="response",
        payload=response_body,
        metadata=metadata,
        handling=handling,
        now=now,
    )


def append_raw_error(
    directory: Path,
    request_id: str,
    request_type: str,
    error: dict[str, Any],
    metadata: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> Path:
    return _append_raw_record(
        directory=directory,
        request_id=request_id,
        request_type=request_type,
        kind="error",
        payload_name="error",
        payload=error,
        metadata=metadata,
        now=now,
    )


def _append_raw_record(
    directory: Path,
    request_id: str,
    request_type: str,
    kind: str,
    payload_name: str,
    payload: Any,
    metadata: dict[str, Any] | None,
    handling: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> Path:
    recorded_at = now or datetime.now().astimezone()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    log_path = directory / f"{LOG_FILE_PREFIX}{recorded_at.date().isoformat()}.jsonl"
    record: dict[str, Any] = {
        "recorded_at": recorded_at.isoformat(),
        "kind": kind,
        "request_id": request_id,
        "request_type": request_type,
        "metadata": metadata or {},
        payload_name: sanitize_raw_payload(payload),
    }
    if handling is not None:
        record["handling"] = sanitize_raw_payload(handling)
    encoded_record = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    with _WRITE_LOCK:
        file_descriptor = os.open(
            log_path,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            0o600,
        )
        try:
            remaining = memoryview(encoded_record)
            while remaining:
                written = os.write(file_descriptor, remaining)
                if written <= 0:
                    raise OSError("写入大模型原始日志失败")
                remaining = remaining[written:]
        finally:
            os.close(file_descriptor)
    return log_path


def sanitize_raw_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: sanitize_raw_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_raw_payload(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_raw_payload(item) for item in value]
    if isinstance(value, str):
        for mime_type in IMAGE_MIME_TYPES:
            prefix = f"data:{mime_type};base64,"
            if value.startswith(prefix):
                encoded_payload = value[len(prefix) :]
                return {
                    "$image_base64_omitted": {
                        "mime_type": mime_type,
                        "encoded_chars": len(encoded_payload),
                        "encoded_sha256": hashlib.sha256(
                            encoded_payload.encode("ascii")
                        ).hexdigest(),
                    }
                }
    return value


def cleanup_raw_request_logs(
    directory: Path,
    retention_days: int,
    today: date | None = None,
) -> int:
    if not directory.is_dir():
        return 0

    days_to_keep = max(1, retention_days)
    current_date = today or datetime.now().astimezone().date()
    oldest_date_to_keep = current_date - timedelta(days=days_to_keep - 1)
    deleted_count = 0

    for log_path in directory.glob(f"{LOG_FILE_PREFIX}*.jsonl"):
        date_text = log_path.stem.removeprefix(LOG_FILE_PREFIX)
        try:
            log_date = date.fromisoformat(date_text)
        except ValueError:
            continue
        if log_date < oldest_date_to_keep:
            log_path.unlink()
            deleted_count += 1

    return deleted_count
