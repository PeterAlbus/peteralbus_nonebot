import asyncio
import json
from datetime import datetime, timezone

import pytest
from multi_llm_chat.context import ContextBuilder, select_recent_event_count
from multi_llm_chat.identity import GroupRosterService
from multi_llm_chat.media import ImageStore
from multi_llm_chat.memory import GroupMemoryStore
from multi_llm_chat.models import ChatEvent, ConversationState


class FakeBot:
    def __init__(self):
        self.calls = []

    async def call_api(self, api, **data):
        self.calls.append((api, data))
        if api == "get_group_info":
            return {"group_id": 100, "group_name": "测试群"}
        if api == "get_group_member_list":
            return [
                {
                    "user_id": 200,
                    "nickname": "小明",
                    "card": "明哥",
                    "role": "admin",
                },
                {
                    "user_id": 201,
                    "nickname": "小红",
                    "card": "",
                    "role": "member",
                },
            ]
        if api == "get_group_member_info":
            return {
                "user_id": data["user_id"],
                "nickname": "刷新昵称",
                "card": "刷新名片",
                "role": "member",
            }
        raise AssertionError(api)


def write_identity_config(path):
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "members": {"200": {"pinned_aliases": ["明先生"]}},
                "groups": {"100": {"append_user_id": True}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_onebot_roster_is_identity_source_for_structured_context(tmp_path):
    identity_path = tmp_path / "identity.json"
    write_identity_config(identity_path)
    roster_service = GroupRosterService(tmp_path, identity_path)
    memory_store = GroupMemoryStore(tmp_path, max_facts=10, max_aliases_per_member=8)
    bot = FakeBot()
    roster = await roster_service.sync_group(bot, "100")
    builder = ContextBuilder(
        roster_service,
        memory_store,
        ImageStore(tmp_path),
        self_knowledge="QQ user_id=2997592724 的群员是你的开发者。",
        char_budget=8000,
        recent_event_min_count=2,
        max_events=10,
    )
    state = ConversationState(
        group_id="100",
        recent_events=[
            ChatEvent(
                event_id="e1",
                group_id="100",
                role="user",
                source="onebot",
                user_id="200",
                display_name="旧名字不会覆盖 roster",
                content="今天吃什么",
                sent_at=datetime(2026, 8, 22, 4, 0, tzinfo=timezone.utc),
            ),
            ChatEvent(
                event_id="e2",
                group_id="100",
                role="assistant",
                source="plugin:nonebot_plugin_whateat_pic",
                content="推荐吃面",
                sent_at=datetime(2026, 8, 22, 4, 1, tzinfo=timezone.utc),
            ),
        ],
    )

    messages = await builder.build(
        "100",
        state,
        turn_mode="passive",
        trigger_event=state.recent_events[0],
        allow_finish_without_reply=True,
    )

    assert roster.group_name == "测试群"
    assert {call[0] for call in bot.calls} == {
        "get_group_info",
        "get_group_member_list",
    }
    member_list_call = next(
        data for api, data in bot.calls if api == "get_group_member_list"
    )
    assert member_list_call == {"group_id": 100}
    assert "明哥 [user_id=200]" in messages[1]["content"]
    assert '"pinned_aliases":["明先生"]' in messages[1]["content"]
    assert "2026-08-22T12:00:00+08:00" in messages[1]["content"]
    assert '"source_plugin":"nonebot_plugin_whateat_pic"' in messages[1]["content"]
    assert '"mode":"passive"' in messages[1]["content"]
    assert '"trigger":{"event_id":"e1"' in messages[1]["content"]
    assert '"content":"今天吃什么"' in messages[1]["content"]
    assert '"messages_since_last_reply":0' in messages[1]["content"]
    assert "user_id=2997592724" in messages[0]["content"]
    assert "最终回复使用群聊纯文本" in messages[0]["content"]
    assert "不要为了延续对话强行反问" in messages[0]["content"]
    assert [message["role"] for message in messages].count("system") == 2
    assert messages[-2]["role"] == "user"
    assert messages[-2]["name"] == "qq_200"
    assert messages[-2]["content"] == "今天吃什么"
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"] == "推荐吃面"


@pytest.mark.asyncio
async def test_direct_context_keeps_empty_trigger_distinct_from_queued_messages(
    tmp_path,
):
    identity_path = tmp_path / "identity.json"
    write_identity_config(identity_path)
    roster_service = GroupRosterService(tmp_path, identity_path)
    builder = ContextBuilder(
        roster_service,
        GroupMemoryStore(tmp_path, max_facts=10, max_aliases_per_member=8),
        ImageStore(tmp_path),
        self_knowledge="QQ user_id=2997592724 的群员是你的开发者。",
        char_budget=8000,
        recent_event_min_count=2,
        max_events=10,
    )
    question = ChatEvent(
        event_id="question",
        group_id="100",
        role="user",
        source="onebot",
        user_id="200",
        display_name="小明",
        content="他怎么笑得那么开心",
        sent_at=datetime(2026, 8, 24, 3, 54, 32, tzinfo=timezone.utc),
    )
    empty_mention = ChatEvent(
        event_id="mention",
        group_id="100",
        role="user",
        source="onebot",
        user_id="200",
        display_name="小明",
        content="",
        sent_at=datetime(2026, 8, 24, 3, 54, 44, tzinfo=timezone.utc),
        to_me=True,
    )
    queued_reply = ChatEvent(
        event_id="reply",
        source_event_id="question",
        group_id="100",
        role="assistant",
        source="llm",
        content="因为他刚打完仗。",
        sent_at=datetime(2026, 8, 24, 3, 54, 53, tzinfo=timezone.utc),
    )
    state = ConversationState(
        group_id="100",
        recent_events=[question, empty_mention, queued_reply],
    )

    messages = await builder.build(
        "100",
        state,
        turn_mode="direct",
        trigger_event=empty_mention,
        allow_finish_without_reply=True,
    )

    runtime = messages[1]["content"]
    assert '"mode":"direct"' in runtime
    assert '"trigger":{"event_id":"mention"' in runtime
    assert '"sender":"用户200 [user_id=200]"' in runtime
    assert '"content":""' in runtime
    assert messages[-1]["role"] == "assistant"
    assert "turn.trigger" in messages[0]["content"]
    assert "触发前最近的连续消息" in messages[0]["content"]
    assert "finish_without_reply" in messages[0]["content"]

    messages_without_finish = await builder.build(
        "100",
        state,
        turn_mode="direct",
        trigger_event=empty_mention,
        allow_finish_without_reply=False,
    )
    assert "finish_without_reply" not in messages_without_finish[0]["content"]
    assert "本轮必须回应触发用户的实际意图" in messages_without_finish[0]["content"]
    assert "优先调用 reply_to_event" in messages_without_finish[0]["content"]
    assert "真正被回答的那条消息" in messages_without_finish[0]["content"]


def test_pinned_aliases_use_user_id_without_group_scope(tmp_path):
    identity_path = tmp_path / "identity.json"
    write_identity_config(identity_path)
    service = GroupRosterService(tmp_path, identity_path)

    assert service.pinned_aliases("200") == ["明先生"]
    assert service.pinned_aliases("201") == []


def test_context_window_selects_whole_recent_events():
    selected = select_recent_event_count(
        [100, 100],
        available_chars=1,
        minimum_count=1,
    )

    assert selected == 1


@pytest.mark.asyncio
async def test_concurrent_sender_updates_do_not_overwrite_other_members(tmp_path):
    identity_path = tmp_path / "identity.json"
    write_identity_config(identity_path)
    service = GroupRosterService(tmp_path, identity_path)

    await asyncio.gather(
        service.update_from_sender("100", "200", {"nickname": "小明"}),
        service.update_from_sender("100", "201", {"nickname": "小红"}),
    )
    roster = await service.get_roster("100")

    assert set(roster.members) == {"200", "201"}
