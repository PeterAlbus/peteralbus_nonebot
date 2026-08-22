import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

LOG_FILE_PREFIX = "llm-requests-"


def append_raw_request(
    directory: Path,
    request_id: str,
    request_type: str,
    request_body: Dict[str, Any],
    metadata: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> Path:
    recorded_at = now or datetime.now().astimezone()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    log_path = directory / f"{LOG_FILE_PREFIX}{recorded_at.date().isoformat()}.jsonl"
    record = {
        "recorded_at": recorded_at.isoformat(),
        "request_id": request_id,
        "request_type": request_type,
        "metadata": metadata or {},
        "request": request_body,
    }
    encoded_record = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    file_descriptor = os.open(
        log_path,
        os.O_APPEND | os.O_CREAT | os.O_WRONLY,
        0o600,
    )
    try:
        os.write(file_descriptor, encoded_record)
    finally:
        os.close(file_descriptor)
    return log_path


def cleanup_raw_request_logs(
    directory: Path,
    retention_days: int,
    today: Optional[date] = None,
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
