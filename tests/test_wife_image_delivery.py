import base64
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import nonebot
import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", ["100", "2997592724"])
async def test_first_and_cached_wife_send_image_content(tmp_path, monkeypatch, user_id):
    nonebot.init(_env_file=None)
    handler = importlib.import_module("peteralbus_wife.handler")
    image = tmp_path / "本地资源" / "图片.png"
    image.parent.mkdir()
    content = b"\x89PNG\r\n\x1a\nimage bytes"
    image.write_bytes(content)
    random_picture = Mock(return_value=("测试角色", image))
    special_picture = Mock(return_value=("指定角色", image))
    finish = AsyncMock()
    monkeypatch.setattr(handler, "wife_manager", handler.WifeManager())
    monkeypatch.setattr(handler, "random_wife_pic", random_picture)
    monkeypatch.setattr(handler, "get_agnes_pic", special_picture)
    monkeypatch.setattr(handler.today_wife, "finish", finish)
    event = SimpleNamespace(group_id=123, user_id=int(user_id))

    await handler.today_wife_handle(event)
    await handler.today_wife_handle(event)

    random_picture.assert_called_once()
    assert special_picture.call_count == (1 if user_id == "2997592724" else 0)
    assert finish.await_count == 2
    for call in finish.await_args_list:
        message = call.args[0]
        (image_segment,) = [segment for segment in message if segment.type == "image"]
        file = image_segment.data["file"]
        assert file.startswith("base64://")
        assert base64.b64decode(file[len("base64://") :]) == content
        assert str(image) not in str(message)
        assert call.kwargs == {"at_sender": True}
    assert "!" in finish.await_args_list[0].args[0].extract_plain_text()
    assert "。" in finish.await_args_list[1].args[0].extract_plain_text()


@pytest.mark.asyncio
async def test_missing_cached_image_returns_resource_error(tmp_path, monkeypatch):
    nonebot.init(_env_file=None)
    handler = importlib.import_module("peteralbus_wife.handler")
    manager = handler.WifeManager()
    manager.record_wife("123", "100", "测试角色", str(tmp_path / "missing.jpg"))
    monkeypatch.setattr(handler, "wife_manager", manager)
    finish = AsyncMock()
    monkeypatch.setattr(handler.today_wife, "finish", finish)

    await handler.today_wife_handle(SimpleNamespace(group_id=123, user_id=100))

    finish.assert_awaited_once_with("老婆图片读取失败，请联系管理员检查资源。")
