from multi_llm_chat.reply_tracker import (
    MatcherExecution,
    OutgoingReplyTracker,
    is_plugin_source,
)


def test_own_plugin_module_name_is_not_external_reply():
    tracker = OutgoingReplyTracker()
    tracker.record(
        MatcherExecution(
            event_id="e1",
            group_id="100",
            source_plugin="plugins.multi_llm_chat",
        ),
        api="send_group_msg",
    )

    assert is_plugin_source("plugins.multi_llm_chat", "multi_llm_chat")
    assert not tracker.has_external_reply("e1", "multi_llm_chat")


def test_non_blocking_plugin_reply_is_linked_to_trigger_event():
    tracker = OutgoingReplyTracker()
    tracker.record(
        MatcherExecution(
            event_id="onebot:100:123",
            group_id="100",
            source_plugin="nonebot_plugin_whateat_pic",
        ),
        api="send_group_msg",
        message_id="456",
    )

    assert tracker.has_external_reply("onebot:100:123", "multi_llm_chat")
    assert tracker.records_for("onebot:100:123")[0].message_id == "456"
