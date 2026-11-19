import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from multi_llm_chat.conversation import ConversationStore
from multi_llm_chat.media import ImageStore
from multi_llm_chat.models import (
    AssistantTurn,
    ChatEvent,
    FunctionCall,
    MemoryPatch,
    ToolCall,
)
from multi_llm_chat.tools import (
    FINISH_WITHOUT_REPLY_TOOL_NAME,
    MENTION_MEMBERS_TOOL_NAME,
    READ_GROUP_IMAGE_TOOL_NAME,
    REPLY_TO_EVENT_TOOL_NAME,
    AgentRunner,
    ReplyDraft,
    ToolArguments,
    ToolDefinition,
    ToolRegistry,
    apply_text,
    build_default_tool_registry,
    strict_model_json_schema,
)
from nonebot.adapters.onebot.v11 import MessageSegment


class AddArguments(ToolArguments):
    left: int
    right: int


def tool_call(name, arguments, call_id):
    return ToolCall(
        id=call_id,
        function=FunctionCall(name=name, arguments=json.dumps(arguments)),
    )


def reply_turn(text):
    return AssistantTurn(
        content="内部推理：下一步调用 send_reply",
        tool_calls=[
            tool_call("add_text", {"text": text}, "text"),
            tool_call("send_reply", {}, "send"),
        ],
    )


class FakeProvider:
    def __init__(self):
        self.calls = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            return AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        function=FunctionCall(
                            name="add",
                            arguments='{"left": 20, "right": 22}',
                        ),
                    )
                ]
            )
        return reply_turn("结果是 42")


class FakeCliRunner:
    def __init__(self, root: Path):
        self.root = root
        self.removed = []

    def create_workspace(self, turn_id):
        workspace = self.root / turn_id
        workspace.mkdir(parents=True)
        return workspace

    def remove_workspace(self, workspace):
        self.removed.append(workspace)


def make_agent_runner(tmp_path, provider, registry, max_steps=2):
    return AgentRunner(
        provider,
        registry,
        FakeCliRunner(tmp_path),
        ConversationStore(tmp_path),
        ImageStore(tmp_path),
        max_steps=max_steps,
    )


@pytest.mark.asyncio
async def test_agent_executes_registered_tool_and_returns_final_answer(tmp_path):
    async def add(context, arguments):
        return {"value": arguments.left + arguments.right}

    registry = ToolRegistry(output_max_chars=2000)
    registry.register(
        ToolDefinition(
            name="add",
            description="加法",
            arguments_model=AddArguments,
            executor=add,
            timeout_seconds=2,
        )
    )
    provider = FakeProvider()
    cli = FakeCliRunner(tmp_path)
    runner = AgentRunner(
        provider,
        registry,
        cli,
        ConversationStore(tmp_path),
        ImageStore(tmp_path),
        max_steps=2,
    )

    result = await runner.run(
        messages=[{"role": "user", "content": "20+22"}],
        turn_id="turn-1",
        group_id="100",
        triggering_user_id="200",
        bot=object(),
        turn_mode="direct",
        allow_finish_without_reply=False,
        replyable_event_ids=[],
        mentionable_user_ids=[],
        readable_image_refs=[],
    )

    assert result.action == "reply"
    assert all(call["tool_choice"] == "required" for call in provider.calls)
    assert result.message.extract_plain_text() == "结果是 42"
    assert result.tool_steps == 1
    second_messages = provider.calls[1]["messages"]
    assert second_messages[-2]["role"] == "assistant"
    assert second_messages[-1]["role"] == "tool"
    assert '"value": 42' in second_messages[-1]["content"]
    assert cli.removed == [tmp_path / "turn-1"]
    direct_tool_names = [
        tool["function"]["name"]
        for tool in provider.calls[0]["tools"]
        if tool["type"] == "function"
    ]
    assert FINISH_WITHOUT_REPLY_TOOL_NAME not in direct_tool_names
    assert REPLY_TO_EVENT_TOOL_NAME not in direct_tool_names
    assert MENTION_MEMBERS_TOOL_NAME not in direct_tool_names
    assert provider.calls[0]["request_type"] == "direct_chat_agent"


@pytest.mark.asyncio
async def test_free_text_without_a_reply_action_cannot_be_sent(tmp_path):
    provider = AsyncMock()
    provider.complete.return_value = AssistantTurn(content="内部推理：调用 mention_members")
    runner = make_agent_runner(tmp_path, provider, ToolRegistry(output_max_chars=2000))
    with pytest.raises(RuntimeError, match="未调用群聊回复工具"):
        await runner.run(
            messages=[], turn_id="free-text", group_id="100", triggering_user_id="200",
            bot=object(), turn_mode="direct", allow_finish_without_reply=False,
            replyable_event_ids=[], mentionable_user_ids=[], readable_image_refs=[],
        )

    assert provider.complete.await_count == 2


@pytest.mark.asyncio
async def test_missing_direct_reply_continues_with_context_and_existing_draft(tmp_path):
    provider = AsyncMock()
    provider.complete.side_effect = [
        AssistantTurn(tool_calls=[tool_call("add_text", {"text": "明天见"}, "draft")]),
        AssistantTurn(content="草稿写好了", reasoning_content="内部判断"),
        AssistantTurn(tool_calls=[tool_call("send_reply", {}, "send")]),
    ]
    runner = make_agent_runner(tmp_path, provider, ToolRegistry(output_max_chars=2000), max_steps=1)
    messages = [{"role": "user", "content": "@小P 明天见？"}]
    result = await runner.run(
        messages=messages, turn_id="direct-followup", group_id="100", triggering_user_id="200",
        bot=object(), turn_mode="direct", allow_finish_without_reply=False,
        replyable_event_ids=[], mentionable_user_ids=[], readable_image_refs=[],
    )
    requests = [call.kwargs for call in provider.complete.await_args_list]
    followup = requests[-1]["messages"]
    assert [r["step"] for r in requests] == [0, 1, 2]
    assert all(r["turn_id"] == "direct-followup" for r in requests)
    assert followup[0] == messages[0]
    assert followup[2]["tool_call_id"] == "draft"
    assert followup[3]["content"] == "草稿写好了"
    assert followup[3]["reasoning_content"] == "内部判断"
    assert followup[-1]["role"] == "system"
    assert "为什么没有回复" in followup[-1]["content"]
    assert result.message.extract_plain_text() == "明天见"


@pytest.mark.parametrize("text", ["", "字" * 4001, "<think>内部推理</think>回答"])
def test_add_text_rejects_invalid_body(text):
    draft = ReplyDraft()
    assert not json.loads(apply_text(draft, json.dumps({"text": text})))["success"]
    assert not draft.message


@pytest.mark.asyncio
async def test_ordered_reply_with_inline_mentions_and_cq_codes(tmp_path):
    provider = AsyncMock()
    provider.complete.side_effect = [
        AssistantTurn(tool_calls=[
            tool_call("add_text", {"text": "请 "}, "a"),
            tool_call("mention_members", {"user_ids": ["200"]}, "b"),
        ]),
        AssistantTurn(content="内部推理", tool_calls=[
            tool_call("add_text", {"text": " 通知 [CQ:at,qq=all]，谢谢 "}, "c"),
            tool_call("mention_members", {"user_ids": ["200"]}, "d"),
            tool_call("add_text", {"text": "！"}, "e"),
            tool_call("send_reply", {}, "f"),
        ]),
    ]
    runner = make_agent_runner(tmp_path, provider, ToolRegistry(output_max_chars=2000))
    result = await runner.run(
        messages=[], turn_id="ordered", group_id="100", triggering_user_id="200",
        bot=object(), turn_mode="direct", allow_finish_without_reply=False,
        replyable_event_ids=[], mentionable_user_ids=["200"], readable_image_refs=[],
    )
    assert list(result.message) == [
        MessageSegment.text("请 "), MessageSegment.at("200"),
        MessageSegment.text(" 通知 "), MessageSegment.at("all"),
        MessageSegment.text("，谢谢 "), MessageSegment.at("200"), MessageSegment.text("！"),
    ]


class ReadImageProvider:
    def __init__(self):
        self.calls = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            return AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="read-image-1",
                        function=FunctionCall(
                            name=READ_GROUP_IMAGE_TOOL_NAME,
                            arguments='{"event_id":"image-event","image_index":0}',
                        ),
                    )
                ]
            )
        return reply_turn("看到了")


@pytest.mark.asyncio
async def test_agent_reads_only_image_reference_exposed_for_current_turn(tmp_path):
    image_store = ImageStore(tmp_path)
    resource = await image_store.store_bytes(
        b"\x89PNG\r\n\x1a\nhistorical",
        placeholder="[图片]",
        content_offset=0,
        media_namespace="onebot:100:image-event",
    )
    conversation_store = ConversationStore(tmp_path)
    await conversation_store.append(
        ChatEvent(
            event_id="image-event",
            group_id="100",
            role="user",
            source="onebot",
            user_id="200",
            content="[图片]",
            images=[resource],
            sent_at=datetime(2026, 8, 27, tzinfo=timezone.utc),
        )
    )
    provider = ReadImageProvider()
    runner = AgentRunner(
        provider,
        ToolRegistry(output_max_chars=2000),
        FakeCliRunner(tmp_path),
        conversation_store,
        image_store,
        max_steps=2,
    )

    result = await runner.run(
        messages=[{"role": "user", "content": "看看前面的图"}],
        turn_id="turn-read-image",
        group_id="100",
        triggering_user_id="200",
        bot=object(),
        turn_mode="direct",
        allow_finish_without_reply=False,
        replyable_event_ids=[],
        mentionable_user_ids=[],
        readable_image_refs=[("image-event", 0)],
    )

    assert result.message.extract_plain_text() == "看到了"
    tool_names = {
        tool["function"]["name"]
        for tool in provider.calls[0]["tools"]
        if tool["type"] == "function"
    }
    assert READ_GROUP_IMAGE_TOOL_NAME in tool_names
    followup_messages = provider.calls[1]["messages"]
    assert followup_messages[-2]["role"] == "tool"
    assert '"event_id": "image-event"' in followup_messages[-2]["content"]
    assert followup_messages[-1]["role"] == "user"
    assert followup_messages[-1]["name"] == "image_tool"
    assert followup_messages[-1]["content"][1]["type"] == "image_url"
    assert followup_messages[-1]["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )


class SkipProvider:
    def __init__(self):
        self.calls = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        return AssistantTurn(
            tool_calls=[
                ToolCall(
                    id="skip-1",
                    function=FunctionCall(
                        name=FINISH_WITHOUT_REPLY_TOOL_NAME,
                        arguments='{"reason":"already_answered"}',
                    ),
                )
            ]
        )


@pytest.mark.asyncio
async def test_passive_agent_can_finish_without_reply_in_one_request(tmp_path):
    provider = SkipProvider()
    runner = make_agent_runner(tmp_path, provider, ToolRegistry(output_max_chars=2000))

    result = await runner.run(
        messages=[{"role": "user", "content": "哈哈"}],
        turn_id="turn-skip",
        group_id="100",
        triggering_user_id="200",
        bot=object(),
        turn_mode="passive",
        allow_finish_without_reply=True,
        replyable_event_ids=[],
        mentionable_user_ids=[],
        readable_image_refs=[],
    )

    assert result.action == "skip"
    assert result.message.extract_plain_text() == ""
    assert result.skip_reason == "already_answered"
    assert len(provider.calls) == 1
    assert provider.calls[0]["request_type"] == "passive_chat_agent"
    tool_names = [
        tool["function"]["name"]
        for tool in provider.calls[0]["tools"]
        if tool["type"] == "function"
    ]
    assert FINISH_WITHOUT_REPLY_TOOL_NAME in tool_names


@pytest.mark.asyncio
async def test_direct_agent_can_finish_when_trigger_was_already_answered(tmp_path):
    provider = SkipProvider()
    runner = make_agent_runner(tmp_path, provider, ToolRegistry(output_max_chars=2000))

    result = await runner.run(
        messages=[
            {"role": "user", "content": "他怎么笑得那么开心"},
            {"role": "user", "content": ""},
            {"role": "assistant", "content": "因为他刚打完仗。"},
        ],
        turn_id="turn-direct-already-answered",
        group_id="100",
        triggering_user_id="200",
        bot=object(),
        turn_mode="direct",
        allow_finish_without_reply=True,
        replyable_event_ids=[],
        mentionable_user_ids=[],
        readable_image_refs=[],
    )

    assert result.action == "skip"
    assert result.message.extract_plain_text() == ""
    assert result.skip_reason == "already_answered"
    assert len(provider.calls) == 1
    assert provider.calls[0]["request_type"] == "direct_chat_agent"


@pytest.mark.asyncio
async def test_direct_agent_rejects_finish_when_no_llm_reply_was_inserted(tmp_path):
    provider = SkipProvider()
    runner = make_agent_runner(tmp_path, provider, ToolRegistry(output_max_chars=2000))

    with pytest.raises(RuntimeError, match="当前轮次不允许无回复终止"):
        await runner.run(
            messages=[{"role": "user", "content": "回答我的问题"}],
            turn_id="turn-direct-must-reply",
            group_id="100",
            triggering_user_id="200",
            bot=object(),
            turn_mode="direct",
            allow_finish_without_reply=False,
            replyable_event_ids=[],
            mentionable_user_ids=[],
            readable_image_refs=[],
        )

    tool_names = [
        tool["function"]["name"]
        for tool in provider.calls[0]["tools"]
        if tool["type"] == "function"
    ]
    assert FINISH_WITHOUT_REPLY_TOOL_NAME not in tool_names


class AddressingProvider:
    def __init__(self):
        self.calls = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            return AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="reply-1",
                        function=FunctionCall(
                            name=REPLY_TO_EVENT_TOOL_NAME,
                            arguments='{"event_id":"event-1"}',
                        ),
                    ),
                    ToolCall(
                        id="mention-1",
                        function=FunctionCall(
                            name=MENTION_MEMBERS_TOOL_NAME,
                            arguments='{"user_ids":["200","201","200"]}',
                        ),
                    ),
                ]
            )
        return reply_turn("我说的是这条消息。")


@pytest.mark.asyncio
async def test_addressing_tools_build_draft_before_send_reply(tmp_path):
    provider = AddressingProvider()
    runner = make_agent_runner(tmp_path, provider, ToolRegistry(output_max_chars=2000))

    result = await runner.run(
        messages=[{"role": "user", "content": "你在说谁？"}],
        turn_id="turn-addressing",
        group_id="100",
        triggering_user_id="200",
        bot=object(),
        turn_mode="direct",
        allow_finish_without_reply=False,
        replyable_event_ids=["event-1"],
        mentionable_user_ids=["200", "201"],
        readable_image_refs=[],
    )

    assert result.action == "reply"
    assert result.message.extract_plain_text() == "我说的是这条消息。"
    assert result.reply_to_event_id == "event-1"
    assert list(result.message) == [
        MessageSegment.at("200"), MessageSegment.at("201"), MessageSegment.at("200"),
        MessageSegment.text("我说的是这条消息。"),
    ]
    tool_names = {
        tool["function"]["name"]
        for tool in provider.calls[0]["tools"]
        if tool["type"] == "function"
    }
    assert REPLY_TO_EVENT_TOOL_NAME in tool_names
    assert MENTION_MEMBERS_TOOL_NAME in tool_names
    second_messages = provider.calls[1]["messages"]
    assert second_messages[-3]["role"] == "assistant"
    assert second_messages[-2]["role"] == "tool"
    assert '"reply_to_event_id": "event-1"' in second_messages[-2]["content"]
    assert second_messages[-1]["role"] == "tool"
    assert '"mention_user_ids": ["200", "201", "200"]' in second_messages[-1]["content"]


class InvalidAddressingProvider:
    def __init__(self):
        self.calls = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            return AssistantTurn(
                tool_calls=[
                    ToolCall(
                        id="reply-invalid",
                        function=FunctionCall(
                            name=REPLY_TO_EVENT_TOOL_NAME,
                            arguments='{"event_id":"other-group-event"}',
                        ),
                    ),
                    ToolCall(
                        id="mention-invalid",
                        function=FunctionCall(
                            name=MENTION_MEMBERS_TOOL_NAME,
                            arguments='{"user_ids":["999"]}',
                        ),
                    ),
                ]
            )
        return reply_turn("不带定向格式的回复")


@pytest.mark.asyncio
async def test_addressing_tools_reject_targets_outside_current_scope(tmp_path):
    provider = InvalidAddressingProvider()
    runner = make_agent_runner(tmp_path, provider, ToolRegistry(output_max_chars=2000))

    result = await runner.run(
        messages=[{"role": "user", "content": "测试非法目标"}],
        turn_id="turn-invalid-addressing",
        group_id="100",
        triggering_user_id="200",
        bot=object(),
        turn_mode="direct",
        allow_finish_without_reply=False,
        replyable_event_ids=["event-1"],
        mentionable_user_ids=["200"],
        readable_image_refs=[],
    )

    assert result.reply_to_event_id is None
    assert list(result.message) == [MessageSegment.text("不带定向格式的回复")]
    second_messages = provider.calls[1]["messages"]
    assert "引用目标不属于当前可回复事件" in second_messages[-2]["content"]
    assert "@目标不是当前群成员: 999" in second_messages[-1]["content"]


@pytest.mark.asyncio
async def test_tool_arguments_are_strictly_validated(tmp_path):
    registry = ToolRegistry(output_max_chars=2000)

    async def add(context, arguments):
        return arguments.left + arguments.right

    registry.register(
        ToolDefinition("add", "加法", AddArguments, add, timeout_seconds=2)
    )
    from multi_llm_chat.tools import ToolContext

    result = await registry.execute(
        "add",
        '{"left": 1, "right": 2, "extra": true}',
        ToolContext("t", "100", "200", object(), tmp_path),
    )

    assert '"success": false' in result
    assert "工具参数校验失败" in result


def test_tool_schema_requires_every_declared_field_for_strict_mode():
    schema = strict_model_json_schema(AddArguments)

    assert schema["additionalProperties"] is False
    assert schema["required"] == ["left", "right"]


def test_strict_schema_inlines_models_and_removes_unsupported_constraints():
    schema_text = str(strict_model_json_schema(MemoryPatch))

    assert "$defs" not in schema_text
    assert "$ref" not in schema_text
    assert "minLength" not in schema_text
    assert "maxItems" not in schema_text


def test_default_tool_registry_does_not_expose_run_cli():
    registry = build_default_tool_registry(
        usage_store=object(),
        balance_query=object(),
        roster_service=object(),
        memory_store=object(),
        tool_timeout_seconds=10,
        output_max_chars=2000,
    )

    tool_names = {
        tool["function"]["name"]
        for tool in registry.schemas()
        if tool["type"] == "function"
    }
    assert tool_names == {
        "get_token_usage",
        "get_balance",
        "get_current_group_info",
        "list_current_group_members",
        "get_current_group_member",
        "search_group_memory",
    }
