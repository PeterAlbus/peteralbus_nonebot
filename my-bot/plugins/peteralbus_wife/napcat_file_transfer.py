"""Transfer local files into NapCat's cache over the existing OneBot connection."""

import asyncio
import base64
import hashlib
import logging
import math
from pathlib import Path
from typing import Any
from uuid import uuid4

CHUNK_BYTES = 256 * 1024
logger = logging.getLogger(__name__)


class NapCatTransferError(RuntimeError):
    pass


def _file_info(path: Path) -> tuple[int, str]:
    size = 0
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    if not size:
        raise NapCatTransferError("待上传文件为空")
    return size, digest.hexdigest()


def _check_response(data: Any, stream_id: str, status: str) -> dict[str, Any]:
    if (
        not isinstance(data, dict)
        or data.get("stream_id") != stream_id
        or data.get("status") != status
    ):
        raise NapCatTransferError(f"NapCat 文件传输响应异常，预期状态 {status}")
    return data


async def transfer_file_to_napcat(
    bot: Any,
    path: Path,
    *,
    retention_seconds: float = 360,
) -> str:
    """Send bounded chunks, verify the completed file and return its remote path.

    The caller owns the overall timeout. NapCat removes incomplete streams after
    a reset, and removes completed cache files after the requested retention.
    """
    size, sha256 = await asyncio.to_thread(_file_info, path)
    total_chunks = (size + CHUNK_BYTES - 1) // CHUNK_BYTES
    stream_id = uuid4().hex
    # NapCat uses filename directly within its shared cache. Never use a source
    # filename here: independent jobs may have the same title, or path separators.
    cache_name = f"nonebot-jm-{stream_id}.pdf"
    retention_ms = math.ceil(max(60, retention_seconds) * 1000)
    started = False
    complete = False
    try:
        with path.open("rb") as file:
            for index in range(total_chunks):
                chunk = await asyncio.to_thread(file.read, CHUNK_BYTES)
                if not chunk:
                    raise NapCatTransferError("传输期间本地文件长度发生变化")
                started = True
                data = _check_response(
                    await bot.call_api(
                        "upload_file_stream",
                        stream_id=stream_id,
                        chunk_data=base64.b64encode(chunk).decode("ascii"),
                        chunk_index=index,
                        total_chunks=total_chunks,
                        file_size=size,
                        expected_sha256=sha256,
                        filename=cache_name,
                        file_retention=retention_ms,
                        _timeout=30,
                    ),
                    stream_id,
                    "chunk_received",
                )
                if (
                    data.get("received_chunks") != index + 1
                    or data.get("total_chunks") != total_chunks
                ):
                    raise NapCatTransferError("NapCat 文件分块接收数量不符")
            if await asyncio.to_thread(file.read, 1):
                raise NapCatTransferError("传输期间本地文件长度发生变化")

        data = _check_response(
            await bot.call_api(
                "upload_file_stream",
                stream_id=stream_id,
                is_complete=True,
                _timeout=30,
            ),
            stream_id,
            "file_complete",
        )
        cached_path = data.get("file_path")
        if (
            data.get("file_size") != size
            or data.get("sha256") != sha256
            or not isinstance(cached_path, str)
            or "\x00" in cached_path
            or cached_path.replace("\\", "/").rsplit("/", 1)[-1] != cache_name
        ):
            raise NapCatTransferError("NapCat 缓存文件的大小、SHA-256 或路径校验失败")
        complete = True
        return cached_path
    finally:
        if started and not complete:
            try:
                # reset deliberately returns an error after cleaning the stream
                # in NapCat v4.18.28. Preserve the original transfer exception.
                await asyncio.wait_for(
                    bot.call_api(
                        "upload_file_stream",
                        stream_id=stream_id,
                        reset=True,
                        _timeout=5,
                    ),
                    timeout=5,
                )
            except Exception as error:  # noqa: BLE001 - cleanup must preserve the transfer error
                logger.debug("NapCat stream reset returned %s", type(error).__name__)


async def upload_group_file_via_napcat(
    bot: Any,
    group_id: int,
    path: Path,
    *,
    timeout: float,
) -> Any:
    """Bound the entire cache-transfer and group-upload operation by one timeout."""

    async def upload() -> Any:
        cached_path = await transfer_file_to_napcat(
            bot, path, retention_seconds=timeout + 60
        )
        return await bot.call_api(
            "upload_group_file",
            group_id=group_id,
            file=cached_path,
            name=path.name,
            _timeout=timeout,
        )

    return await asyncio.wait_for(upload(), timeout=timeout)
