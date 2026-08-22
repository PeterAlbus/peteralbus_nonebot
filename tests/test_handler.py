import importlib
from types import SimpleNamespace

import nonebot
import pytest
from multi_llm_chat.models import ImageResource
from nonebot.adapters.onebot.v11 import Message, MessageSegment


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
