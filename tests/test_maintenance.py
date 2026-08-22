import json
from datetime import datetime, timezone

import pytest
from multi_llm_chat.conversation import ConversationStore
from multi_llm_chat.identity import GroupRosterService
from multi_llm_chat.maintenance import ConversationMaintainer
from multi_llm_chat.media import ImageStore
from multi_llm_chat.memory import GroupMemoryStore
from multi_llm_chat.models import AssistantTurn, ChatEvent


class FakeProvider:
    def __init__(self):
        self.calls = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        return AssistantTurn(
            content=json.dumps(
                {
                    "alias_observations": [
                        {
                            "user_id": "200",
                            "alias": "阿明",
                            "evidence_event_ids": ["e1"],
                        }
                    ],
                    "member_observations": [],
                    "add_facts": [],
                },
                ensure_ascii=False,
            )
        )

    def image_understanding_enabled(self):
        return False


class Logger:
    def warning(self, *args, **kwargs):
        pass


class CompressionContext:
    def split_for_compression(self, events):
        return list(events[:1]), list(events[1:])


class CompressionProvider:
    def __init__(self):
        self.calls = []

    def image_understanding_enabled(self):
        return True

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["request_type"] == "memory_maintenance":
            return AssistantTurn(
                content=json.dumps(
                    {
                        "alias_observations": [],
                        "member_observations": [],
                        "add_facts": [],
                    }
                )
            )
        return AssistantTurn(
            content=json.dumps(
                {
                    "topics": ["图片话题"],
                    "decisions": [],
                    "unresolved_questions": [],
                    "temporary_context": [],
                }
            )
        )


@pytest.mark.asyncio
async def test_memory_maintenance_uses_json_patch_and_evidence_validation(tmp_path):
    identity_path = tmp_path / "identity.json"
    identity_path.write_text('{"version": 1, "groups": {}}', encoding="utf-8")
    provider = FakeProvider()
    memory_store = GroupMemoryStore(tmp_path, max_facts=10, max_aliases_per_member=8)
    maintainer = ConversationMaintainer(
        provider=provider,
        context_builder=object(),
        conversation_store=object(),
        memory_store=memory_store,
        image_store=ImageStore(tmp_path),
        roster_service=GroupRosterService(tmp_path, identity_path),
        summary_max_chars=2000,
        logger=Logger(),
    )
    event = ChatEvent(
        event_id="e1",
        group_id="100",
        role="user",
        source="onebot",
        user_id="200",
        content="以后叫我阿明",
        sent_at=datetime.now(timezone.utc),
    )

    await maintainer._extract_memory("100", [event], "turn-1")

    memory = await memory_store.get("100")
    assert memory.members["200"].learned_aliases[0].value == "阿明"
    assert provider.calls[0]["response_format"] == {"type": "json_object"}
    assert "tools" not in provider.calls[0]


@pytest.mark.asyncio
async def test_successful_compression_sends_image_then_deletes_resource(tmp_path):
    identity_path = tmp_path / "identity.json"
    identity_path.write_text(
        '{"version": 1, "members": {}, "groups": {}}',
        encoding="utf-8",
    )
    image_store = ImageStore(tmp_path)
    resource = await image_store.store_bytes(
        b"\x89PNG\r\n\x1a\ncompressed-image",
        placeholder="[图片]",
        content_offset=0,
        media_namespace="onebot:100:image-event",
    )
    first = ChatEvent(
        event_id="image-event",
        group_id="100",
        role="user",
        source="onebot",
        user_id="200",
        content="[图片]",
        images=[resource],
        sent_at=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc),
    )
    retained = ChatEvent(
        event_id="retained",
        group_id="100",
        role="user",
        source="onebot",
        user_id="200",
        content="后续消息",
        sent_at=datetime(2026, 8, 22, 12, 1, tzinfo=timezone.utc),
    )
    conversation_store = ConversationStore(tmp_path)
    await conversation_store.append(first)
    await conversation_store.append(retained)
    provider = CompressionProvider()
    maintainer = ConversationMaintainer(
        provider=provider,
        context_builder=CompressionContext(),
        conversation_store=conversation_store,
        memory_store=GroupMemoryStore(
            tmp_path,
            max_facts=10,
            max_aliases_per_member=8,
        ),
        image_store=image_store,
        roster_service=GroupRosterService(tmp_path, identity_path),
        summary_max_chars=2000,
        logger=Logger(),
    )

    state = await maintainer.maintain_if_needed(
        "100",
        await conversation_store.get("100"),
    )

    assert [event.event_id for event in state.recent_events] == ["retained"]
    assert not (tmp_path / "media" / resource.media_key).exists()
    compression_call = next(
        call
        for call in provider.calls
        if call["request_type"] == "conversation_compression"
    )
    compression_content = compression_call["messages"][1]["content"]
    assert isinstance(compression_content, list)
    assert any(
        item.get("type") == "image_url"
        and item["image_url"]["url"].startswith("data:image/png;base64,")
        for item in compression_content
    )
