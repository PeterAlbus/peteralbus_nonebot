"""Persistent, provider-reported usage shared by the bot and operations API."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_tokens",
    "reasoning_tokens",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_usage(usage: dict[str, Any]) -> dict[str, int | None]:
    def number(*values: Any) -> int | None:
        return next((v for v in values if type(v) is int and v >= 0), None)

    def detail(name: str, key: str) -> Any:
        value = usage.get(name)
        return value.get(key) if isinstance(value, dict) else None

    incoming = number(usage.get("input_tokens"), usage.get("prompt_tokens"))
    outgoing = number(usage.get("output_tokens"), usage.get("completion_tokens"))
    total = number(usage.get("total_tokens"))
    if total is None and incoming is not None and outgoing is not None:
        total = incoming + outgoing
    return {
        "input_tokens": incoming,
        "output_tokens": outgoing,
        "total_tokens": total,
        "cached_tokens": number(
            usage.get("prompt_cache_hit_tokens"),
            detail("prompt_tokens_details", "cached_tokens"),
            detail("input_tokens_details", "cached_tokens"),
        ),
        "reasoning_tokens": number(
            detail("completion_tokens_details", "reasoning_tokens"),
            detail("output_tokens_details", "reasoning_tokens"),
        ),
    }


class UsageStore:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().absolute()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            db.execute(
                "INSERT OR IGNORE INTO metadata VALUES ('tracking_started_at', ?)",
                (utc_now(),),
            )
            db.execute("""CREATE TABLE IF NOT EXISTS usage_requests (
                request_id TEXT NOT NULL, attempt INTEGER NOT NULL,
                started_at TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
                request_type TEXT NOT NULL, status TEXT NOT NULL,
                input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER,
                cached_tokens INTEGER, reasoning_tokens INTEGER,
                sdk_retries INTEGER NOT NULL DEFAULT 0, raw_usage TEXT,
                PRIMARY KEY (request_id, attempt)
            )""")
            db.execute(
                "CREATE INDEX IF NOT EXISTS usage_started_at ON usage_requests(started_at)"
            )

    def begin(
        self,
        request_id: str,
        attempt: int,
        provider: str,
        model: str,
        request_type: str,
        started_at: str | None = None,
    ) -> None:
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute(
                """INSERT INTO usage_requests
                (request_id, attempt, started_at, provider, model, request_type, status)
                VALUES (?, ?, ?, ?, ?, ?, 'pending')""",
                (
                    request_id,
                    attempt,
                    started_at or utc_now(),
                    provider,
                    model,
                    request_type,
                ),
            )

    def finish(
        self,
        request_id: str,
        attempt: int,
        usage: dict[str, Any],
        status: str,
        sdk_retries: int = 0,
    ) -> None:
        counts = normalize_usage(usage)
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute(
                """UPDATE usage_requests SET status=?, input_tokens=?, output_tokens=?,
                total_tokens=?, cached_tokens=?, reasoning_tokens=?, sdk_retries=?, raw_usage=?
                WHERE request_id=? AND attempt=?""",
                (
                    status,
                    *(counts[k] for k in TOKEN_FIELDS),
                    sdk_retries,
                    json.dumps(usage, ensure_ascii=False),
                    request_id,
                    attempt,
                ),
            )

    def query(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
        provider: str | None = None,
        model: str | None = None,
        request_type: str | None = None,
    ) -> dict[str, Any]:
        end = end_date or datetime.now(SHANGHAI).date()
        start = start_date or end
        if start > end:
            raise ValueError("start_date must not be later than end_date")
        lower = (
            datetime.combine(start, time.min, SHANGHAI)
            .astimezone(timezone.utc)
            .isoformat()
        )
        upper = (
            datetime.combine(end + timedelta(days=1), time.min, SHANGHAI)
            .astimezone(timezone.utc)
            .isoformat()
        )
        clauses = ["started_at >= ?", "started_at < ?"]
        values: list[Any] = [lower, upper]
        for field, value in (
            ("provider", provider),
            ("model", model),
            ("request_type", request_type),
        ):
            if value is not None:
                clauses.append(f"{field} = ?")
                values.append(value)
        with closing(
            sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=10)
        ) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            tracking_row = db.execute(
                "SELECT value FROM metadata WHERE key='tracking_started_at'"
            ).fetchone()
            if tracking_row is None:
                raise sqlite3.DatabaseError("Usage tracking metadata is missing")
            tracking = tracking_row[0]
            aggregates = ", ".join(f"SUM({field}) AS {field}" for field in TOKEN_FIELDS)
            rows = db.execute(
                "SELECT date(started_at, '+8 hours') AS date, provider, model, request_type, "
                "COUNT(*) AS requests, "
                "SUM(input_tokens IS NULL OR output_tokens IS NULL OR total_tokens IS NULL) AS unknown_usage_requests, "
                "SUM(status = 'pending') AS pending_requests, SUM(status = 'error') AS failed_requests, "
                "SUM(sdk_retries) AS sdk_retries_without_usage, "
                + aggregates
                + " FROM usage_requests WHERE "
                + " AND ".join(clauses)
                + " GROUP BY date, provider, model, request_type ORDER BY date, provider, model, request_type",
                values,
            ).fetchall()
            as_of = utc_now()
        total = self._empty_counts()
        for row in rows:
            for field in total:
                if row[field] is not None:
                    total[field] = (total[field] or 0) + row[field]
        return {
            "source": "local_response_usage",
            "scope": "bot",
            "timezone": "Asia/Shanghai",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "as_of": as_of,
            "tracking_started_at": tracking,
            "filters": {
                "provider": provider,
                "model": model,
                "request_type": request_type,
            },
            "totals": total,
            "breakdown": [dict(row) for row in rows],
        }

    @staticmethod
    def _empty_counts() -> dict[str, Any]:
        return {
            "requests": 0,
            "unknown_usage_requests": 0,
            "pending_requests": 0,
            "failed_requests": 0,
            "sdk_retries_without_usage": 0,
            **dict.fromkeys(TOKEN_FIELDS),
        }
