from datetime import datetime, timedelta, timezone

import pytest
from multi_llm_chat.memory import GroupMemoryStore, MemoryValidationError
from multi_llm_chat.models import AliasObservation, ChatEvent, FactProposal, MemoryPatch


def chat_event(event_id: str, content: str, days_ago: int = 0) -> ChatEvent:
    return ChatEvent(
        event_id=event_id,
        group_id="100",
        role="user",
        source="onebot",
        user_id="200",
        content=content,
        sent_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )


@pytest.mark.asyncio
async def test_one_member_can_learn_multiple_aliases_without_replacement(tmp_path):
    store = GroupMemoryStore(tmp_path, max_facts=10, max_aliases_per_member=8)
    events = [chat_event("e1", "以后叫我阿明"), chat_event("e2", "明哥也可以")]
    patch = MemoryPatch(
        alias_observations=[
            AliasObservation(user_id="200", alias="阿明", evidence_event_ids=["e1"]),
            AliasObservation(user_id="200", alias="明哥", evidence_event_ids=["e2"]),
        ]
    )

    memory = await store.apply_patch("100", patch, events, {"200"})

    assert {alias.value for alias in memory.members["200"].learned_aliases} == {
        "阿明",
        "明哥",
    }


@pytest.mark.asyncio
async def test_alias_without_literal_evidence_is_rejected_atomically(tmp_path):
    store = GroupMemoryStore(tmp_path, max_facts=10, max_aliases_per_member=8)
    patch = MemoryPatch(
        alias_observations=[
            AliasObservation(user_id="200", alias="不存在", evidence_event_ids=["e1"])
        ]
    )

    with pytest.raises(MemoryValidationError):
        await store.apply_patch(
            "100",
            patch,
            [chat_event("e1", "普通消息")],
            {"200"},
        )

    assert (await store.get("100")).members == {}


@pytest.mark.asyncio
async def test_recent_facts_expire_and_memory_is_bounded(tmp_path):
    store = GroupMemoryStore(tmp_path, max_facts=2, max_aliases_per_member=8)
    old_event = chat_event("old", "很久以前的活动", days_ago=40)
    old_patch = MemoryPatch(
        add_facts=[
            FactProposal(
                category="recent_event",
                content="参加了很久以前的活动",
                importance=3,
                source_event_ids=["old"],
            )
        ]
    )
    await store.apply_patch("100", old_patch, [old_event], {"200"})

    assert (await store.get("100")).recent_facts == []

    new_events = [chat_event(f"e{i}", f"事实 {i}") for i in range(3)]
    new_patch = MemoryPatch(
        add_facts=[
            FactProposal(
                category="recent_event",
                content=f"关键事实 {i}",
                importance=i + 1,
                source_event_ids=[f"e{i}"],
            )
            for i in range(3)
        ]
    )
    memory = await store.apply_patch("100", new_patch, new_events, {"200"})

    assert [fact.content for fact in memory.recent_facts] == [
        "关键事实 2",
        "关键事实 1",
    ]


@pytest.mark.asyncio
async def test_pinned_aliases_and_learned_aliases_are_rendered_together(tmp_path):
    store = GroupMemoryStore(tmp_path, max_facts=10, max_aliases_per_member=8)
    event = chat_event("e1", "叫我阿明")
    await store.apply_patch(
        "100",
        MemoryPatch(
            alias_observations=[
                AliasObservation(user_id="200", alias="阿明", evidence_event_ids=["e1"])
            ]
        ),
        [event],
        {"200"},
    )

    rendered = await store.render_context("100", {"200"}, {"200": ["明先生"]})

    assert "明先生、阿明" in rendered
