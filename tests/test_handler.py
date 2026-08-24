import asyncio
import importlib
from datetime import datetime, timezone
from types import SimpleNamespace

import nonebot
import pytest
from multi_llm_chat.models import ChatEvent, ConversationState, ImageResource
from multi_llm_chat.reply_tracker import OutgoingReplyTracker
from multi_llm_chat.tools import AgentRunResult
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment


def make_group_event(message: Message) -> GroupMessageEvent:
    return GroupMessageEvent(
        time=1_777_000_000,
        self_id=2_436_220_150,
        post_type="message",
        sub_type="normal",
        user_id=2_997_592_724,
        message_type="group",
        message_id=123,
        message=message,
        raw_message=str(message),
        font=0,
        sender={
            "user_id": 2_997_592_724,
            "nickname": "Peter",
            "card": "",
            "role": "member",
        },
        group_id=708_695_087,
    )


def test_get_connected_bot_normalizes_onebot_self_id(monkeypatch) -> None:
    nonebot.init()
    handler = importlib.import_module("multi_llm_chat.handler")
    requested_self_ids = []
    expected_bot = object()

    def fake_get_bot(self_id):
        requested_self_ids.append(self_id)
        return expected_bot

    monkeypatch.setattr(handler, "get_bot", fake_get_bot)

    assert handler._get_connected_bot(2436220150) is expected_bot
    assert requested_self_ids == ["2436220150"]


@pytest.mark.asyncio
async def test_incoming_image_is_downloaded_independently_of_model_setting(
    monkeypatch,
) -> None:
    nonebot.init()
    handler = importlib.import_module("multi_llm_chat.handler")
    calls = []

    async def fake_download(url, placeholder, content_offset, media_namespace):
        calls.append((url, placeholder, content_offset, media_namespace))
        return ImageResource(
            media_key="ab/" + "a" * 64 + ".png",
            mime_type="image/png",
            size=16,
            sha256="a" * 64,
            content_offset=content_offset,
            placeholder=placeholder,
        )

    monkeypatch.setattr(handler.image_store, "download", fake_download)
    monkeypatch.setattr(
        handler,
        "config",
        SimpleNamespace(llm_chat_image_understanding=False),
    )
    message = Message(
        [
            MessageSegment.text("看看"),
            MessageSegment(
                "image",
                {
                    "file": "image.png",
                    "url": "https://multimedia.nt.qq.com.cn/image.png",
                    "summary": "测试图",
                },
            ),
        ]
    )

    content, images = await handler.ingest_message_content(message, "onebot:100:1")

    assert content == "看看[图片:测试图]"
    assert len(images) == 1
    assert calls == [
        (
            "https://multimedia.nt.qq.com.cn/image.png",
            "[图片:测试图]",
            2,
            "onebot:100:1",
        )
    ]


def test_at_bot_between_image_and_text_is_an_immediate_message() -> None:
    nonebot.init()
    handler = importlib.import_module("multi_llm_chat.handler")
    event = make_group_event(
        Message(
            [
                MessageSegment(
                    "image",
                    {
                        "file": "image.png",
                        "url": "https://multimedia.nt.qq.com.cn/image.png",
                    },
                ),
                MessageSegment.at(2_436_220_150),
                MessageSegment.text(" 看看这张图"),
            ]
        )
    )

    assert event.to_me is False
    assert handler.is_directed_at_bot(event) is True
    assert handler._mentioned_user_ids(event) == []


def test_onebot_addressing_metadata_is_extracted_from_original_message() -> None:
    nonebot.init()
    handler = importlib.import_module("multi_llm_chat.handler")
    event = make_group_event(
        Message(
            [
                MessageSegment("reply", {"id": "321"}),
                MessageSegment.at(123_456_789),
                MessageSegment.text(" 你怎么看"),
            ]
        )
    )

    assert handler._mentioned_user_ids(event) == ["123456789"]
    assert handler._reply_to_message_id(event) == "321"


@pytest.mark.asyncio
async def test_general_handler_does_not_ingest_message_directed_at_bot(
    monkeypatch,
) -> None:
    nonebot.init()
    handler = importlib.import_module("multi_llm_chat.handler")
    event = make_group_event(
        Message(
            [
                MessageSegment(
                    "image",
                    {
                        "file": "image.png",
                        "url": "https://multimedia.nt.qq.com.cn/image.png",
                    },
                ),
                MessageSegment.at(2_436_220_150),
            ]
        )
    )
    ingested = False

    async def fake_ingest_event(*args, **kwargs):
        nonlocal ingested
        ingested = True

    monkeypatch.setattr(handler, "_ingest_event", fake_ingest_event)

    await handler.handle_message(event, event.message)

    assert ingested is False


@pytest.mark.asyncio
async def test_other_matcher_reply_is_recorded_from_nonebot_context(
    monkeypatch,
) -> None:
    nonebot.init()
    handler = importlib.import_module("multi_llm_chat.handler")
    event = make_group_event(Message(MessageSegment.text("今天中午吃什么")))
    stored_events = []

    class FakeConversationStore:
        async def append(self, event):
            stored_events.append(event)

    tracker = OutgoingReplyTracker()
    monkeypatch.setattr(handler, "conversation_store", FakeConversationStore())
    monkeypatch.setattr(handler, "reply_tracker", tracker)
    pending_passive_task = asyncio.create_task(asyncio.Event().wait())
    monkeypatch.setattr(
        handler,
        "_passive_tasks",
        {str(event.group_id): pending_passive_task},
    )
    matcher_token = handler.nonebot_current_matcher.set(
        SimpleNamespace(plugin_name="nonebot_plugin_whateat_pic")
    )
    event_token = handler.nonebot_current_event.set(event)
    try:
        task = asyncio.create_task(
            handler.track_outgoing_group_message(
                bot=object(),
                exception=None,
                api="send_group_msg",
                data={
                    "group_id": event.group_id,
                    "message": "推荐吃面",
                },
                result={"message_id": 456},
            )
        )
        await task
        await asyncio.sleep(0)
    finally:
        handler.nonebot_current_event.reset(event_token)
        handler.nonebot_current_matcher.reset(matcher_token)

    event_id = "onebot:708695087:123"
    assert tracker.has_external_reply(event_id, "multi_llm_chat")
    assert len(stored_events) == 1
    assert stored_events[0].source_event_id == event_id
    assert stored_events[0].source == "plugin:nonebot_plugin_whateat_pic"
    assert stored_events[0].role == "assistant"
    assert stored_events[0].content == "推荐吃面"
    assert pending_passive_task.cancelled()


@pytest.mark.asyncio
async def test_passive_reply_builds_context_and_runs_agent_once(monkeypatch) -> None:
    nonebot.init()
    handler = importlib.import_module("multi_llm_chat.handler")
    trigger = ChatEvent(
        event_id="onebot:708695087:123",
        group_id="708695087",
        role="user",
        source="onebot",
        user_id="2997592724",
        display_name="Peter",
        content="今天中午吃什么",
        sent_at=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc),
    )
    state = ConversationState(group_id=trigger.group_id, recent_events=[trigger])
    stored_events = []
    context_calls = []
    agent_calls = []

    class FakeConversationStore:
        async def get(self, group_id):
            assert group_id == trigger.group_id
            return state

        async def append(self, event):
            stored_events.append(event)

    class FakeContextBuilder:
        async def build(self, *args, **kwargs):
            context_calls.append((args, kwargs))
            return [{"role": "user", "content": trigger.content}]

    class FakeAgentRunner:
        async def run(self, **kwargs):
            agent_calls.append(kwargs)
            return AgentRunResult(
                action="reply",
                content="吃面吧",
                tool_steps=0,
            )

    class FakeProvider:
        def image_understanding_enabled(self):
            return False

    class FakeBot:
        def __init__(self):
            self.calls = []

        async def call_api(self, api, **data):
            self.calls.append((api, data))
            return {"message_id": 456}

    bot = FakeBot()
    monkeypatch.setattr(handler, "conversation_store", FakeConversationStore())
    monkeypatch.setattr(handler, "context_builder", FakeContextBuilder())
    monkeypatch.setattr(handler, "agent_runner", FakeAgentRunner())
    monkeypatch.setattr(handler, "provider", FakeProvider())
    monkeypatch.setattr(handler, "reply_tracker", OutgoingReplyTracker())
    monkeypatch.setattr(
        handler,
        "_passive_tasks",
        {trigger.group_id: asyncio.current_task()},
    )

    await handler._process_reply(bot=bot, trigger_event=trigger, passive=True)

    assert len(context_calls) == 1
    assert context_calls[0][1]["turn_mode"] == "passive"
    assert context_calls[0][1]["trigger_event"] is trigger
    assert context_calls[0][1]["allow_finish_without_reply"] is True
    assert len(agent_calls) == 1
    assert agent_calls[0]["turn_mode"] == "passive"
    assert agent_calls[0]["allow_finish_without_reply"] is True
    assert bot.calls == [
        (
            "send_group_msg",
            {"group_id": 708695087, "message": "吃面吧"},
        )
    ]
    assert len(stored_events) == 1
    assert stored_events[0].source_event_id == trigger.event_id
    assert stored_events[0].content == "吃面吧"


def test_direct_finish_requires_llm_reply_after_trigger() -> None:
    nonebot.init()
    handler = importlib.import_module("multi_llm_chat.handler")
    trigger = ChatEvent(
        event_id="trigger",
        group_id="708695087",
        role="user",
        source="onebot",
        user_id="2997592724",
        content="",
        sent_at=datetime(2026, 8, 24, 3, 54, 44, tzinfo=timezone.utc),
        to_me=True,
    )
    user_message = ChatEvent(
        event_id="later-user",
        group_id=trigger.group_id,
        role="user",
        source="onebot",
        user_id="10001",
        content="插入的群友消息",
        sent_at=datetime(2026, 8, 24, 3, 54, 45, tzinfo=timezone.utc),
    )
    plugin_reply = ChatEvent(
        event_id="plugin-reply",
        group_id=trigger.group_id,
        role="assistant",
        source="plugin:nonebot_plugin_whateat_pic",
        content="插件回复",
        sent_at=datetime(2026, 8, 24, 3, 54, 46, tzinfo=timezone.utc),
    )
    llm_reply = ChatEvent(
        event_id="llm-reply",
        group_id=trigger.group_id,
        role="assistant",
        source="llm",
        content="小P排队期间插入的回复",
        sent_at=datetime(2026, 8, 24, 3, 54, 47, tzinfo=timezone.utc),
    )

    assert not handler._has_llm_reply_after_trigger(
        ConversationState(group_id=trigger.group_id, recent_events=[trigger]),
        trigger,
    )
    assert not handler._has_llm_reply_after_trigger(
        ConversationState(
            group_id=trigger.group_id,
            recent_events=[trigger, user_message, plugin_reply],
        ),
        trigger,
    )
    assert handler._has_llm_reply_after_trigger(
        ConversationState(
            group_id=trigger.group_id,
            recent_events=[trigger, user_message, plugin_reply, llm_reply],
        ),
        trigger,
    )
