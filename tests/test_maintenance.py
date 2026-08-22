import json
from datetime import datetime, timezone

import pytest
from multi_llm_chat.identity import GroupRosterService
from multi_llm_chat.maintenance import ConversationMaintainer
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


class Logger:
    def warning(self, *args, **kwargs):
        pass


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
