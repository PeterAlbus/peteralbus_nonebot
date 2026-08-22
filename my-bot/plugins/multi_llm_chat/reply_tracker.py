import time
from dataclasses import dataclass
from typing import Dict, List


@dataclass(frozen=True)
class MatcherExecution:
    event_id: str
    group_id: str
    source_plugin: str


@dataclass(frozen=True)
class ReplyRecord:
    event_id: str
    group_id: str
    source_plugin: str
    api: str
    sent_at_monotonic: float
    message_id: str = ""


def is_plugin_source(source_plugin: str, plugin_name: str) -> bool:
    normalized = source_plugin.replace("-", "_")
    expected = plugin_name.replace("-", "_")
    return normalized == expected or normalized.endswith(f".{expected}")


class OutgoingReplyTracker:
    def __init__(self, retention_seconds: float = 300.0) -> None:
        self._retention_seconds = retention_seconds
        self._records: Dict[str, List[ReplyRecord]] = {}

    def record(
        self,
        execution: MatcherExecution,
        api: str,
        message_id: str = "",
    ) -> ReplyRecord:
        self._prune()
        record = ReplyRecord(
            event_id=execution.event_id,
            group_id=execution.group_id,
            source_plugin=execution.source_plugin,
            api=api,
            sent_at_monotonic=time.monotonic(),
            message_id=message_id,
        )
        self._records.setdefault(execution.event_id, []).append(record)
        return record

    def has_external_reply(self, event_id: str, own_plugin: str) -> bool:
        self._prune()
        return any(
            not is_plugin_source(record.source_plugin, own_plugin)
            for record in self._records.get(event_id, [])
        )

    def records_for(self, event_id: str) -> List[ReplyRecord]:
        self._prune()
        return list(self._records.get(event_id, []))

    def _prune(self) -> None:
        cutoff = time.monotonic() - self._retention_seconds
        expired = [
            event_id
            for event_id, records in self._records.items()
            if not records or max(item.sent_at_monotonic for item in records) < cutoff
        ]
        for event_id in expired:
            del self._records[event_id]
