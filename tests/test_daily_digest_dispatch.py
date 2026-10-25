import asyncio
import importlib
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import nonebot
import pytest
from fastapi import HTTPException
from multi_llm_chat.daily_digest import DailyDigestConfig


@pytest.fixture
def dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CHAT_STATE_DIR", str(tmp_path / "initial-handler-state"))
    nonebot.init()
    handler = importlib.import_module("multi_llm_chat.handler")
    monkeypatch.setattr(handler, "state_dir", tmp_path)
    monkeypatch.setattr(
        handler,
        "daily_digest_config",
        DailyDigestConfig.model_validate(
            {
                "groups": [{"group_id": "123"}, {"group_id": "456", "enabled": False}],
            }
        ),
    )
    monkeypatch.setattr(handler, "_daily_digest_locks", {})
    monkeypatch.setattr(handler, "_reply_lock", lambda _: asyncio.Lock())
    monkeypatch.setattr(handler, "_cancel_passive_task", Mock())
    monkeypatch.setattr(handler, "_ensure_conversation_maintenance", Mock())
    service = SimpleNamespace(build_message=AsyncMock(return_value="今日日报\n测试"))
    monkeypatch.setattr(handler, "daily_digest_service", service)
    store = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(recent_events=[])),
        append=AsyncMock(return_value=object()),
    )
    monkeypatch.setattr(handler, "conversation_store", store)
    bot = SimpleNamespace(call_api=AsyncMock(return_value={"message_id": 99}))
    getter = Mock(return_value=bot)
    monkeypatch.setattr(handler, "get_bot", getter)
    return handler, service, store, bot, getter


@pytest.mark.asyncio
async def test_preview_has_no_send_or_history_side_effects(dispatch):
    handler, service, store, bot, getter = dispatch
    result = await handler.run_daily_digest("123")
    assert result["status"] == "preview"
    assert result["message"] == "今日日报\n测试"
    service.build_message.assert_awaited_once()
    getter.assert_not_called()
    bot.call_api.assert_not_awaited()
    store.append.assert_not_awaited()
    assert not (handler.state_dir / "daily_digest_receipts").exists()


@pytest.mark.asyncio
async def test_manual_send_receipt_prevents_duplicate_and_force_allows_resend(dispatch):
    handler, service, _, bot, _ = dispatch
    result = await handler.run_daily_digest("123", send=True)
    assert result["status"] == "sent"
    assert result["message_id"] == "99"
    assert result["receipt_persisted"] is True
    assert result["history_recorded"] is True
    bot.call_api.assert_awaited_once_with(
        "send_group_msg", group_id=123, message="今日日报\n测试"
    )
    assert (await handler.run_daily_digest("123", send=True))[
        "status"
    ] == "already_sent"
    assert service.build_message.await_count == 1
    assert (await handler.run_daily_digest("123", send=True, force=True))[
        "status"
    ] == "sent"
    assert bot.call_api.await_count == 2


@pytest.mark.asyncio
async def test_sent_receipt_survives_history_failure(dispatch):
    handler, _, store, bot, _ = dispatch
    store.append.side_effect = OSError("disk failure")
    result = await handler.run_daily_digest("123", send=True)
    assert result["status"] == "sent"
    assert result["history_recorded"] is False
    assert (await handler.run_daily_digest("123", send=True))[
        "status"
    ] == "already_sent"
    assert bot.call_api.await_count == 1


@pytest.mark.asyncio
async def test_older_history_receipt_is_recognized(dispatch):
    handler, service, store, bot, _ = dispatch
    store.get.return_value = SimpleNamespace(
        recent_events=[
            SimpleNamespace(
                source="llm:daily_digest",
                platform_message_id="old-1",
                sent_at=datetime.now().astimezone(),
            )
        ]
    )
    assert (await handler.run_daily_digest("123", send=True))["message_id"] == "old-1"
    service.build_message.assert_not_awaited()
    bot.call_api.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_or_unknown_group_and_offline_bot_do_not_send(dispatch):
    handler, service, _, bot, getter = dispatch
    for group_id in ["456", "789"]:
        with pytest.raises(HTTPException) as error:
            await handler.run_daily_digest(group_id, send=True)
        assert error.value.status_code == 404
    getter.side_effect = ValueError("offline")
    with pytest.raises(HTTPException) as error:
        await handler.run_daily_digest("123", send=True)
    assert error.value.status_code == 503
    service.build_message.assert_not_awaited()
    bot.call_api.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_manual_trigger_is_rejected(dispatch):
    handler, service, _, _, _ = dispatch
    started = asyncio.Event()
    release = asyncio.Event()

    async def build(group):
        started.set()
        await release.wait()
        return "今日日报"

    service.build_message.side_effect = build
    task = asyncio.create_task(handler.run_daily_digest("123"))
    await started.wait()
    try:
        with pytest.raises(HTTPException) as error:
            await handler.run_daily_digest("123", send=True)
        assert error.value.status_code == 409
    finally:
        release.set()
        await task
