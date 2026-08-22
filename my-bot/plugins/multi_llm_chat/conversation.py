import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .json_store import atomic_write_json_model, load_json_model
from .models import ChatEvent, ConversationState, ConversationSummary


class ConversationStore:
    def __init__(self, state_dir: Path) -> None:
        self._directory = state_dir / "conversations"
        self._locks: Dict[str, asyncio.Lock] = {}

    def _path(self, group_id: str) -> Path:
        return self._directory / f"{group_id}.json"

    def _lock(self, group_id: str) -> asyncio.Lock:
        return self._locks.setdefault(group_id, asyncio.Lock())

    def _load(self, group_id: str) -> ConversationState:
        return load_json_model(
            self._path(group_id),
            ConversationState,
            lambda: ConversationState(group_id=group_id),
        )

    async def get(self, group_id: str) -> ConversationState:
        async with self._lock(group_id):
            return await asyncio.to_thread(self._load, group_id)

    async def append(self, event: ChatEvent) -> ConversationState:
        async with self._lock(event.group_id):
            state = await asyncio.to_thread(self._load, event.group_id)
            if any(item.event_id == event.event_id for item in state.recent_events):
                return state
            state.recent_events.append(event)
            state.recent_events.sort(key=lambda item: item.sent_at)
            await asyncio.to_thread(
                atomic_write_json_model,
                self._path(event.group_id),
                state,
            )
            return state

    async def replace_compressed_history(
        self,
        group_id: str,
        summary: ConversationSummary,
        retained_events: Sequence[ChatEvent],
    ) -> ConversationState:
        async with self._lock(group_id):
            current = await asyncio.to_thread(self._load, group_id)
            retained_ids = {event.event_id for event in retained_events}
            events_arrived_during_compression = [
                event
                for event in current.recent_events
                if event.event_id not in retained_ids
                and event.sent_at > summary.covered_to
            ]
            merged_events = [*retained_events, *events_arrived_during_compression]
            merged_events.sort(key=lambda event: event.sent_at)
            state = ConversationState(
                group_id=group_id,
                rolling_summary=summary,
                recent_events=merged_events,
            )
            await asyncio.to_thread(
                atomic_write_json_model,
                self._path(group_id),
                state,
            )
            return state

    async def find_events(
        self,
        group_id: str,
        event_ids: Sequence[str],
    ) -> Dict[str, ChatEvent]:
        state = await self.get(group_id)
        wanted = set(event_ids)
        return {
            event.event_id: event
            for event in state.recent_events
            if event.event_id in wanted
        }


def event_message_for_model(
    event: ChatEvent,
    content: Optional[Any] = None,
) -> Dict[str, Any]:
    if event.role == "assistant":
        return {
            "role": "assistant",
            "content": event.content if content is None else content,
        }
    return {
        "role": "user",
        "name": f"qq_{event.user_id}" if event.user_id else "qq_unknown",
        "content": event.content if content is None else content,
    }


def event_metadata_for_model(
    event: ChatEvent,
    display_name: Optional[str] = None,
    append_user_id: bool = False,
) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {
        "event_id": event.event_id,
        "source_event_id": event.source_event_id,
        "role": event.role,
        "source": event.source,
        "sent_at": event.sent_at.astimezone().isoformat(),
        "directed_to_bot": event.to_me,
        "mentioned_user_ids": event.mentioned_user_ids,
        "reply_to_message_id": event.reply_to_message_id,
    }
    if event.role == "user":
        name = display_name or event.display_name or f"用户{event.user_id or 'unknown'}"
        metadata["sender"] = (
            f"{name} [user_id={event.user_id}]"
            if append_user_id and event.user_id
            else name
        )
        metadata["user_id"] = event.user_id
    if event.source.startswith("plugin:"):
        metadata["source_plugin"] = event.source.removeprefix("plugin:")
    return metadata


def summary_to_text(summary: Optional[ConversationSummary]) -> str:
    if summary is None:
        return "暂无已压缩的历史对话。"
    sections = [
        (
            f"覆盖时间：{summary.covered_from.isoformat()} "
            f"至 {summary.covered_to.isoformat()}"
        ),
        "正在或曾经讨论：\n" + _format_items(summary.topics),
        "已经形成的决定：\n" + _format_items(summary.decisions),
        "尚未解决的问题：\n" + _format_items(summary.unresolved_questions),
        "仍需保留的短期语境：\n" + _format_items(summary.temporary_context),
    ]
    return "\n\n".join(sections)


def _format_items(items: List[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else "- 无"


def event_time_range(events: Sequence[ChatEvent]) -> Optional[Sequence[datetime]]:
    if not events:
        return None
    return events[0].sent_at, events[-1].sent_at
