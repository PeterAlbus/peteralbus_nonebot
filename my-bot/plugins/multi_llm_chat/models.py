from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MemberIdentityConfig(StrictModel):
    pinned_aliases: List[str] = Field(default_factory=list)


class GroupIdentityConfig(StrictModel):
    append_user_id: bool = False


class IdentityConfig(StrictModel):
    version: Literal[1] = 1
    members: Dict[str, MemberIdentityConfig] = Field(default_factory=dict)
    groups: Dict[str, GroupIdentityConfig] = Field(default_factory=dict)


class RosterMember(StrictModel):
    user_id: str
    nickname: str = ""
    card: str = ""
    role: str = "member"
    join_time: int = 0
    last_sent_time: int = 0
    title: str = ""


class GroupRoster(StrictModel):
    version: Literal[1] = 1
    group_id: str
    group_name: str = ""
    synced_at: datetime
    members: Dict[str, RosterMember] = Field(default_factory=dict)


class ImageResource(StrictModel):
    media_key: str
    mime_type: Literal[
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
        "image/bmp",
    ]
    size: int = Field(gt=0)
    sha256: str = Field(min_length=64, max_length=64)
    content_offset: int = Field(ge=0)
    placeholder: str


class ChatEvent(StrictModel):
    event_id: str
    source_event_id: Optional[str] = None
    group_id: str
    role: Literal["user", "assistant"]
    source: str
    user_id: Optional[str] = None
    display_name: str = ""
    content: str
    images: List[ImageResource] = Field(default_factory=list)
    sent_at: datetime
    to_me: bool = False


class ConversationSummary(StrictModel):
    covered_from: datetime
    covered_to: datetime
    topics: List[str] = Field(default_factory=list)
    decisions: List[str] = Field(default_factory=list)
    unresolved_questions: List[str] = Field(default_factory=list)
    temporary_context: List[str] = Field(default_factory=list)


class ConversationSummaryContent(StrictModel):
    topics: List[str] = Field(default_factory=list)
    decisions: List[str] = Field(default_factory=list)
    unresolved_questions: List[str] = Field(default_factory=list)
    temporary_context: List[str] = Field(default_factory=list)


class ReplyDecision(StrictModel):
    should_reply: bool
    reason: str = ""


class ConversationState(StrictModel):
    version: Literal[1] = 1
    group_id: str
    rolling_summary: Optional[ConversationSummary] = None
    recent_events: List[ChatEvent] = Field(default_factory=list)


class LearnedAlias(StrictModel):
    value: str
    normalized_value: str
    first_seen_at: datetime
    last_seen_at: datetime
    evidence_count: int = 1
    confidence: float = 0.45
    evidence_event_ids: List[str] = Field(default_factory=list)


class MemoryAttribute(StrictModel):
    value: str
    first_seen_at: datetime
    last_seen_at: datetime
    evidence_count: int = 1
    evidence_event_ids: List[str] = Field(default_factory=list)


class MemberMemory(StrictModel):
    learned_aliases: List[LearnedAlias] = Field(default_factory=list)
    traits: List[MemoryAttribute] = Field(default_factory=list)
    interests: List[MemoryAttribute] = Field(default_factory=list)


class MemoryFact(StrictModel):
    id: str
    category: Literal[
        "preference",
        "relationship",
        "ongoing_topic",
        "decision",
        "commitment",
        "recent_event",
    ]
    content: str
    involved_user_ids: List[str] = Field(default_factory=list)
    importance: int = Field(ge=1, le=5)
    happened_at: datetime
    last_confirmed_at: datetime
    expires_at: datetime
    source_event_ids: List[str] = Field(default_factory=list)


class GroupMemory(StrictModel):
    version: Literal[1] = 1
    group_id: str
    updated_at: datetime
    members: Dict[str, MemberMemory] = Field(default_factory=dict)
    recent_facts: List[MemoryFact] = Field(default_factory=list)


class AliasObservation(StrictModel):
    user_id: str
    alias: str = Field(min_length=1, max_length=40)
    evidence_event_ids: List[str] = Field(min_length=1, max_length=8)


class MemberObservation(StrictModel):
    user_id: str
    traits_to_add: List[str] = Field(default_factory=list, max_length=4)
    interests_to_add: List[str] = Field(default_factory=list, max_length=4)
    evidence_event_ids: List[str] = Field(min_length=1, max_length=8)


class FactProposal(StrictModel):
    category: Literal[
        "preference",
        "relationship",
        "ongoing_topic",
        "decision",
        "commitment",
        "recent_event",
    ]
    content: str = Field(min_length=1, max_length=240)
    involved_user_ids: List[str] = Field(default_factory=list, max_length=8)
    importance: int = Field(ge=1, le=5)
    source_event_ids: List[str] = Field(min_length=1, max_length=8)


class MemoryPatch(StrictModel):
    alias_observations: List[AliasObservation] = Field(default_factory=list)
    member_observations: List[MemberObservation] = Field(default_factory=list)
    add_facts: List[FactProposal] = Field(default_factory=list)


class FunctionCall(StrictModel):
    name: str
    arguments: str


class ToolCall(StrictModel):
    id: str
    type: Literal["function"] = "function"
    function: FunctionCall


class AssistantTurn(StrictModel):
    content: str = ""
    tool_calls: List[ToolCall] = Field(default_factory=list)
    reasoning_content: str = ""
    finish_reason: str = ""
    usage: Dict[str, Any] = Field(default_factory=dict)

    def as_message(self) -> Dict[str, Any]:
        message: Dict[str, Any] = {
            "role": "assistant",
            "content": self.content or None,
        }
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in self.tool_calls
            ]
        return message
