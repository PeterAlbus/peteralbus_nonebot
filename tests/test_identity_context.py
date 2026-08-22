import asyncio
import json
from datetime import datetime, timezone

import pytest
from multi_llm_chat.context import ContextBuilder, select_recent_event_messages
from multi_llm_chat.identity import GroupRosterService
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
                "members": {
                    "200": {"pinned_aliases": ["明先生"]}
                },
                "groups": {
                    "100": {
                        "append_user_id": True
                    }
                },
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

    messages = await builder.build("100", state)

    assert roster.group_name == "测试群"
    assert {call[0] for call in bot.calls} == {
        "get_group_info",
        "get_group_member_list",
    }
    member_list_call = next(
        data for api, data in bot.calls if api == "get_group_member_list"
    )
    assert member_list_call == {"group_id": 100}
    assert any("明哥 [user_id=200]" in item["content"] for item in messages)
    assert any("人工初始称呼：明先生" in item["content"] for item in messages)
    assert messages[-4]["role"] == "system"
    assert "2026-08-22T12:00:00+08:00" in messages[-4]["content"]
    assert messages[-3]["role"] == "user"
    assert messages[-3]["name"] == "qq_200"
    assert messages[-3]["content"] == "今天吃什么"
    assert messages[-2]["role"] == "system"
    assert (
        '"source_plugin":"nonebot_plugin_whateat_pic"'
        in messages[-2]["content"]
    )
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"] == "推荐吃面"


def test_pinned_aliases_use_user_id_without_group_scope(tmp_path):
    identity_path = tmp_path / "identity.json"
    write_identity_config(identity_path)
    service = GroupRosterService(tmp_path, identity_path)

    assert service.pinned_aliases("200") == ["明先生"]
    assert service.pinned_aliases("201") == []


def test_context_window_keeps_event_metadata_and_body_together():
    first = [
        {"role": "system", "content": "first metadata"},
        {"role": "user", "content": "first body"},
    ]
    second = [
        {"role": "system", "content": "second metadata"},
        {"role": "assistant", "content": "second body"},
    ]

    selected = select_recent_event_messages(
        [first, second],
        available_chars=1,
        minimum_count=1,
    )

    assert selected == second


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
