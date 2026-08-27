from datetime import datetime, timezone
from pathlib import Path

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
    ToolArguments,
    ToolDefinition,
    ToolRegistry,
    strict_model_json_schema,
)


class AddArguments(ToolArguments):
    left: int
    right: int


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
        return AssistantTurn(content="结果是 42")


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
    assert result.content == "结果是 42"
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
        return AssistantTurn(content="看到了")


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

    assert result.content == "看到了"
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
    assert result.content == ""
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
    assert result.content == ""
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
        return AssistantTurn(content="我说的是这条消息。")


@pytest.mark.asyncio
async def test_addressing_tools_build_draft_before_plain_text_reply(tmp_path):
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
    assert result.content == "我说的是这条消息。"
    assert result.reply_to_event_id == "event-1"
    assert result.mention_user_ids == ("200", "201")
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
    assert '"mention_user_ids": ["200", "201"]' in second_messages[-1]["content"]


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
        return AssistantTurn(content="不带定向格式的回复")


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
    assert result.mention_user_ids == ()
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
