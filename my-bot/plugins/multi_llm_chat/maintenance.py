import json
from typing import Any, List, Sequence, Set
from uuid import uuid4

from pydantic import ValidationError

from .context import ContextBuilder
from .conversation import ConversationStore, summary_to_text
from .identity import GroupRosterService
from .memory import GroupMemoryStore
from .models import (
    ChatEvent,
    ConversationState,
    ConversationSummary,
    ConversationSummaryContent,
    MemoryPatch,
)
from .prompts import COMPRESSION_SYSTEM_PROMPT, MEMORY_MAINTENANCE_SYSTEM_PROMPT
from .provider import LLMProvider


class ConversationMaintainer:
    def __init__(
        self,
        provider: LLMProvider,
        context_builder: ContextBuilder,
        conversation_store: ConversationStore,
        memory_store: GroupMemoryStore,
        roster_service: GroupRosterService,
        summary_max_chars: int,
        logger: Any,
    ) -> None:
        self._provider = provider
        self._context_builder = context_builder
        self._conversation_store = conversation_store
        self._memory_store = memory_store
        self._roster_service = roster_service
        self._summary_max_chars = max(1000, summary_max_chars)
        self._logger = logger

    async def maintain_if_needed(
        self,
        group_id: str,
        state: ConversationState,
    ) -> ConversationState:
        compressible, retained = self._context_builder.split_for_compression(
            state.recent_events
        )
        if not compressible:
            return state
        turn_id = uuid4().hex
        try:
            await self._extract_memory(group_id, compressible, turn_id)
        except Exception as error:
            self._logger.warning(
                "群聊记忆维护失败: group_id={}, turn_id={}, error_type={}",
                group_id,
                turn_id,
                type(error).__name__,
            )
        summary = await self._compress(state, compressible, turn_id)
        return await self._conversation_store.replace_compressed_history(
            group_id,
            summary,
            retained,
        )

    async def _compress(
        self,
        state: ConversationState,
        events: Sequence[ChatEvent],
        turn_id: str,
    ) -> ConversationSummary:
        payload = {
            "previous_summary": summary_to_text(state.rolling_summary),
            "events": [event.model_dump(mode="json") for event in events],
        }
        turn = await self._provider.complete(
            messages=[
                {"role": "system", "content": COMPRESSION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
                },
            ],
            request_type="conversation_compression",
            turn_id=turn_id,
            step=0,
            response_format={"type": "json_object"},
        )
        content = ConversationSummaryContent.model_validate_json(turn.content)
        bounded = _bound_summary(content, self._summary_max_chars)
        covered_from = (
            state.rolling_summary.covered_from
            if state.rolling_summary is not None
            else events[0].sent_at
        )
        return ConversationSummary(
            covered_from=covered_from,
            covered_to=events[-1].sent_at,
            **bounded.model_dump(),
        )

    async def _extract_memory(
        self,
        group_id: str,
        events: Sequence[ChatEvent],
        turn_id: str,
    ) -> None:
        turn = await self._provider.complete(
            messages=[
                {"role": "system", "content": MEMORY_MAINTENANCE_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        [event.model_dump(mode="json") for event in events],
                        ensure_ascii=False,
                    ),
                },
            ],
            request_type="memory_maintenance",
            turn_id=turn_id,
            step=0,
            response_format={"type": "json_object"},
        )
        try:
            patch = MemoryPatch.model_validate_json(turn.content)
        except ValidationError as error:
            self._logger.warning(
                "记忆 patch 校验失败: turn_id={}, error_count={}",
                turn_id,
                error.error_count(),
            )
            return
        roster = await self._roster_service.get_roster(group_id)
        valid_user_ids: Set[str] = set(roster.members)
        valid_user_ids.update(
            event.user_id for event in events if event.user_id is not None
        )
        try:
            await self._memory_store.apply_patch(
                group_id,
                patch,
                events,
                valid_user_ids,
            )
        except ValueError as error:
            self._logger.warning(
                "记忆 patch 被拒绝: turn_id={}, error_type={}",
                turn_id,
                type(error).__name__,
            )


def _bound_summary(
    content: ConversationSummaryContent,
    max_chars: int,
) -> ConversationSummaryContent:
    result = ConversationSummaryContent(
        topics=_bound_items(content.topics),
        decisions=_bound_items(content.decisions),
        unresolved_questions=_bound_items(content.unresolved_questions),
        temporary_context=_bound_items(content.temporary_context),
    )
    while len(result.model_dump_json()) > max_chars:
        candidates: List[List[str]] = [
            result.temporary_context,
            result.topics,
            result.unresolved_questions,
            result.decisions,
        ]
        target = next((items for items in candidates if items), None)
        if target is None:
            break
        target.pop()
    return result


def _bound_items(items: Sequence[str]) -> List[str]:
    return [item.strip()[:300] for item in items[:12] if item.strip()]
