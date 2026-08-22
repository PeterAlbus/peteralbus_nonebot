from datetime import datetime, timedelta, timezone

import pytest
from multi_llm_chat.conversation import (
    ConversationStore,
    format_event_for_model,
    summary_to_text,
)
from multi_llm_chat.models import ChatEvent, ConversationSummary


def event(event_id: str, minute: int, role: str = "user") -> ChatEvent:
    return ChatEvent(
        event_id=event_id,
        group_id="100",
        role=role,
        source="onebot" if role == "user" else "llm",
        user_id="200" if role == "user" else None,
        display_name="小明",
        content=f"消息 {event_id}",
        sent_at=datetime(2026, 8, 22, 12, minute, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_conversation_keeps_events_until_engineered_compression(tmp_path):
    store = ConversationStore(tmp_path)
    for index in range(100):
        await store.append(
            ChatEvent(
                event_id=f"event-{index}",
                group_id="100",
                role="user",
                source="onebot",
                user_id="200",
                content=str(index),
                sent_at=datetime(2026, 8, 22, tzinfo=timezone.utc)
                + timedelta(seconds=index),
            )
        )
    await store.append(event("event-99", 1))

    state = await store.get("100")

    assert len(state.recent_events) == 100
    assert state.recent_events[0].event_id == "event-0"


@pytest.mark.asyncio
async def test_compression_preserves_events_that_arrived_after_covered_range(tmp_path):
    store = ConversationStore(tmp_path)
    first = event("first", 0)
    retained = event("retained", 1)
    arrived = event("arrived", 2)
    for item in (first, retained, arrived):
        await store.append(item)
    summary = ConversationSummary(
        covered_from=first.sent_at,
        covered_to=first.sent_at,
        topics=["午饭"],
    )

    state = await store.replace_compressed_history("100", summary, [retained])

    assert [item.event_id for item in state.recent_events] == ["retained", "arrived"]
    assert "午饭" in summary_to_text(state.rolling_summary)


def test_model_messages_keep_real_roles_time_and_optional_id():
    user_message = format_event_for_model(
        event("user", 3),
        display_name="小明",
        append_user_id=True,
    )
    assistant_message = format_event_for_model(event("bot", 4, role="assistant"))

    assert user_message["role"] == "user"
    assert user_message["name"] == "qq_200"
    assert "2026-08-22 20:03:00 +0800" in user_message["content"]
    assert "小明 [user_id=200]" in user_message["content"]
    assert assistant_message["role"] == "assistant"
    assert "source=llm" in assistant_message["content"]
