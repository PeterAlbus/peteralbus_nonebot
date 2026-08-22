import importlib

import nonebot


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
