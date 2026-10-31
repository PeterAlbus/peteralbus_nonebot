import asyncio
import base64
import hashlib
import importlib
from unittest.mock import AsyncMock

import nonebot
import pytest
from peteralbus_wife import napcat_file_transfer as transfer


class CacheBot:
    """Minimal backend contract: receive bytes, verify a digest, return a cache path."""

    def __init__(self):
        self.calls = []
        self.streams = {}
        self.cached = {}

    async def call_api(self, action, **params):
        self.calls.append((action, params))
        if action == "upload_group_file":
            assert params["file"] in self.cached
            return {"file_id": "group-file-id"}
        stream_id = params["stream_id"]
        if params.get("reset"):
            self.streams.pop(stream_id, None)
            raise RuntimeError("Stream reset completed")
        state = self.streams.setdefault(stream_id, {"chunks": [], "params": params})
        first = state["params"]
        if params.get("is_complete"):
            content = b"".join(state["chunks"])
            digest = hashlib.sha256(content).hexdigest()
            assert digest == first["expected_sha256"]
            path = "/QQ/cache/" + first["filename"]
            self.cached[path] = content
            return {
                "stream_id": stream_id,
                "status": "file_complete",
                "type": "response",
                "file_path": path,
                "file_size": len(content),
                "sha256": digest,
            }
        assert params["chunk_index"] == len(state["chunks"])
        state["chunks"].append(base64.b64decode(params["chunk_data"]))
        return {
            "stream_id": stream_id,
            "status": "chunk_received",
            "type": "stream",
            "received_chunks": len(state["chunks"]),
            "total_chunks": first["total_chunks"],
        }


@pytest.mark.asyncio
async def test_pdf_bytes_are_staged_before_group_upload_with_original_name(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(transfer, "CHUNK_BYTES", 8)
    path = tmp_path / "带空格的 文件.pdf"
    content = b"%PDF-1.3\n" + bytes(range(33))
    path.write_bytes(content)
    bot = CacheBot()

    assert await transfer.upload_group_file_via_napcat(bot, 123, path, timeout=300) == {
        "file_id": "group-file-id"
    }

    action, params = bot.calls[-1]
    assert action == "upload_group_file"
    assert params["group_id"] == 123
    assert params["name"] == path.name
    assert params["_timeout"] == 300
    assert params["file"] != str(path)
    assert bot.cached[params["file"]] == content
    chunks = [p for action, p in bot.calls if p.get("chunk_data")]
    assert all(len(base64.b64decode(p["chunk_data"])) <= 8 for p in chunks)
    assert chunks[0]["file_retention"] >= 360000
    assert all(str(path) not in str(p) for _, p in bot.calls)


@pytest.mark.asyncio
async def test_same_named_files_get_independent_remote_cache_paths(tmp_path):
    path = tmp_path / "same.pdf"
    path.write_bytes(b"%PDF-1.3\nhello")
    bot = CacheBot()
    paths = await asyncio.gather(
        *(transfer.transfer_file_to_napcat(bot, path) for _ in range(2))
    )
    assert paths[0] != paths[1]
    assert all(bot.cached[p] == path.read_bytes() for p in paths)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_field,value",
    [
        ("sha256", "wrong"),
        ("file_size", 1),
        ("file_path", ""),
        ("file_path", "/somewhere/original.pdf"),
        ("stream_id", "other-stream"),
        ("status", "chunk_received"),
    ],
)
async def test_invalid_completion_never_causes_group_upload(tmp_path, bad_field, value):
    path = tmp_path / "test.pdf"
    path.write_bytes(b"%PDF-1.3\nhello")
    bot = CacheBot()
    original = bot.call_api

    async def call_api(action, **params):
        response = await original(action, **params)
        if params.get("is_complete"):
            response[bad_field] = value
        return response

    bot.call_api = call_api
    with pytest.raises(transfer.NapCatTransferError):
        await transfer.upload_group_file_via_napcat(bot, 123, path, timeout=30)
    assert not any(action == "upload_group_file" for action, _ in bot.calls)
    assert bot.calls[-1][1]["reset"] is True
    assert path.exists()


@pytest.mark.asyncio
async def test_partial_transfer_failure_resets_only_its_own_stream(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(transfer, "CHUNK_BYTES", 4)
    path = tmp_path / "test.pdf"
    path.write_bytes(b"abcdefghijk")
    bot = CacheBot()
    original = bot.call_api

    async def call_api(action, **params):
        if params.get("chunk_index") == 1:
            raise ConnectionError("disconnected")
        return await original(action, **params)

    bot.call_api = call_api
    with pytest.raises(ConnectionError, match="disconnected"):
        await transfer.upload_group_file_via_napcat(bot, 123, path, timeout=30)
    assert bot.streams == {}
    assert bot.cached == {}
    assert bot.calls[-1][1]["reset"] is True
    assert bot.calls[-1][1]["stream_id"] == bot.calls[0][1]["stream_id"]
    assert not any(action == "clean_stream_temp_file" for action, _ in bot.calls)


@pytest.mark.asyncio
async def test_invalid_chunk_ack_does_not_finalize_or_upload(tmp_path):
    path = tmp_path / "test.pdf"
    path.write_bytes(b"hello")
    bot = CacheBot()
    original = bot.call_api

    async def call_api(action, **params):
        data = await original(action, **params)
        data["received_chunks"] = 0
        return data

    bot.call_api = call_api
    with pytest.raises(transfer.NapCatTransferError, match="数量不符"):
        await transfer.upload_group_file_via_napcat(bot, 123, path, timeout=30)
    assert not any(
        p.get("is_complete") or a == "upload_group_file" for a, p in bot.calls
    )


@pytest.mark.asyncio
async def test_empty_file_never_calls_napcat(tmp_path):
    path = tmp_path / "empty.pdf"
    path.touch()
    bot = CacheBot()
    with pytest.raises(transfer.NapCatTransferError, match="为空"):
        await transfer.transfer_file_to_napcat(bot, path)
    assert bot.calls == []


@pytest.mark.asyncio
async def test_overall_timeout_cancels_transfer_and_resets_stream(tmp_path):
    path = tmp_path / "test.pdf"
    path.write_bytes(b"hello")
    started = asyncio.Event()
    calls = []

    async def call_api(action, **params):
        calls.append((action, params))
        if not params.get("reset"):
            started.set()
            await asyncio.Event().wait()
        raise RuntimeError("Stream reset completed")

    bot = type("Bot", (), {"call_api": staticmethod(call_api)})()
    with pytest.raises(asyncio.TimeoutError):
        await transfer.upload_group_file_via_napcat(bot, 123, path, timeout=0.5)
    assert started.is_set()
    assert calls[-1][1]["reset"] is True
    assert not any(action == "upload_group_file" for action, _ in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_jm_handler_uses_transfer_and_preserves_failed_local_pdf(
    tmp_path, monkeypatch, fails
):
    nonebot.init(_env_file=None)
    downloader = importlib.import_module("peteralbus_wife.jm_downloader")
    root = tmp_path / "jobs"
    job = root / "jm-test"
    job.mkdir(parents=True)
    pdf = job / "test.pdf"
    pdf.write_bytes(b"%PDF-1.3\nhello")
    result = type("Result", (), {"duration": 1})()
    monkeypatch.setattr(downloader, "_prepare_job", lambda _: (object(), root, job))
    monkeypatch.setattr(downloader, "_cleanup_stale_jobs", lambda _: None)
    monkeypatch.setattr(downloader, "_select_pdf_path", lambda *args: pdf)
    monkeypatch.setattr(
        downloader, "download_album_async", AsyncMock(return_value=result)
    )
    monkeypatch.setattr(downloader, "_send_status", AsyncMock())
    upload = AsyncMock(
        side_effect=RuntimeError("cache transfer failed") if fails else None
    )
    monkeypatch.setattr(downloader, "upload_group_file_via_napcat", upload)
    event = type("Event", (), {"group_id": 123, "user_id": 456})()
    args = type("Args", (), {"extract_plain_text": lambda self: "123456"})()
    bot = object()

    await downloader.handle_jm_download(bot, event, args)

    upload.assert_awaited_once_with(bot, 123, pdf, timeout=300)
    assert pdf.exists() is fails
    assert "123456" not in downloader._active_jobs
