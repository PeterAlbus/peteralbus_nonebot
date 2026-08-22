import asyncio
import base64
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .models import ChatEvent, ImageResource

MAX_BASE64_CHARS = 50 * 1024 * 1024
MAX_IMAGE_BYTES = (MAX_BASE64_CHARS // 4) * 3
DOWNLOAD_TIMEOUT_SECONDS = 30
CHUNK_SIZE = 64 * 1024

IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}


class ImageDownloadError(RuntimeError):
    pass


class ImageStore:
    def __init__(self, state_dir: Path) -> None:
        self._directory = state_dir / "media"

    async def download(
        self,
        url: str,
        placeholder: str,
        content_offset: int,
        media_namespace: str,
    ) -> ImageResource:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ImageDownloadError("图片 URL 必须是有效的 HTTPS 地址")

        try:
            data = await asyncio.to_thread(_download_image, url)
        except ImageDownloadError:
            raise
        except (OSError, TimeoutError, URLError) as error:
            raise ImageDownloadError("下载图片失败") from error

        return await self.store_bytes(
            data,
            placeholder=placeholder,
            content_offset=content_offset,
            media_namespace=media_namespace,
        )

    async def store_bytes(
        self,
        data: bytes,
        placeholder: str,
        content_offset: int,
        media_namespace: str,
    ) -> ImageResource:
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise ImageDownloadError("图片大小无效")
        mime_type = detect_image_mime_type(data)
        if mime_type is None:
            raise ImageDownloadError("图片格式不受支持")
        digest = hashlib.sha256(data).hexdigest()
        namespace_digest = hashlib.sha256(media_namespace.encode("utf-8")).hexdigest()
        media_key = (
            f"{namespace_digest[:2]}/{namespace_digest}/"
            f"{digest}{IMAGE_EXTENSIONS[mime_type]}"
        )
        path = self._resolve_media_key(media_key)
        if not path.is_file():
            await asyncio.to_thread(_atomic_write, path, data)
        return ImageResource(
            media_key=media_key,
            mime_type=mime_type,
            size=len(data),
            sha256=digest,
            content_offset=content_offset,
            placeholder=placeholder,
        )

    async def data_urls(self, images: Sequence[ImageResource]) -> List[str]:
        return [await asyncio.to_thread(self._data_url, image) for image in images]

    async def build_content(
        self,
        event: ChatEvent,
        include_images: bool,
    ) -> Any:
        if not include_images or not event.images:
            return event.content
        data_urls = await self.data_urls(event.images)
        return build_multimodal_content(event.content, event.images, data_urls)

    async def build_payload_content(
        self,
        text: str,
        events: Sequence[ChatEvent],
        include_images: bool,
    ) -> Any:
        if not include_images or not any(event.images for event in events):
            return text
        content: List[Dict[str, Any]] = [{"type": "text", "text": text}]
        for event in events:
            if not event.images:
                continue
            data_urls = await self.data_urls(event.images)
            content.append(
                {
                    "type": "text",
                    "text": f"以下图片来自 event_id={event.event_id}：",
                }
            )
            content.extend(
                {"type": "image_url", "image_url": {"url": data_url}}
                for data_url in data_urls
            )
        return content

    async def delete_compressed_images(
        self,
        compressed_events: Sequence[ChatEvent],
        retained_events: Sequence[ChatEvent],
    ) -> int:
        retained_keys = {
            image.media_key for event in retained_events for image in event.images
        }
        removable_keys = {
            image.media_key
            for event in compressed_events
            for image in event.images
            if image.media_key not in retained_keys
        }
        deleted = 0
        for media_key in removable_keys:
            path = self._resolve_media_key(media_key)
            try:
                await asyncio.to_thread(path.unlink)
            except FileNotFoundError:
                continue
            deleted += 1
            try:
                await asyncio.to_thread(path.parent.rmdir)
            except OSError:
                pass
        return deleted

    def _data_url(self, image: ImageResource) -> str:
        path = self._resolve_media_key(image.media_key)
        data = path.read_bytes()
        if len(data) != image.size or hashlib.sha256(data).hexdigest() != image.sha256:
            raise ImageDownloadError("图片资源校验失败")
        encoded = base64.b64encode(data).decode("ascii")
        if len(encoded) > MAX_BASE64_CHARS:
            raise ImageDownloadError("图片超过 Base64 输入大小限制")
        return f"data:{image.mime_type};base64,{encoded}"

    def _resolve_media_key(self, media_key: str) -> Path:
        root = self._directory.resolve()
        path = (root / media_key).resolve()
        try:
            path.relative_to(root)
        except ValueError as error:
            raise ValueError("非法图片资源路径") from error
        return path


def build_multimodal_content(
    text: str,
    images: Sequence[ImageResource],
    data_urls: Sequence[str],
) -> List[Dict[str, Any]]:
    if len(images) != len(data_urls):
        raise ValueError("图片资源和传输内容数量不一致")
    content: List[Dict[str, Any]] = []
    cursor = 0
    for image, data_url in sorted(
        zip(images, data_urls), key=lambda item: item[0].content_offset
    ):
        start = image.content_offset
        end = start + len(image.placeholder)
        if start < cursor or text[start:end] != image.placeholder:
            raise ValueError("图片占位信息与消息正文不一致")
        if start > cursor:
            content.append({"type": "text", "text": text[cursor:start]})
        content.append({"type": "image_url", "image_url": {"url": data_url}})
        cursor = end
    if cursor < len(text):
        content.append({"type": "text", "text": text[cursor:]})
    return content


def detect_image_mime_type(data: bytes) -> Optional[str]:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    return None


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".image-", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _download_image(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "peteralbus-nonebot/1.0"})
    with urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
        final_url = str(response.geturl())
        if urlsplit(final_url).scheme != "https":
            raise ImageDownloadError("图片下载重定向到非 HTTPS 地址")
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError as error:
                raise ImageDownloadError("图片响应长度无效") from error
            if declared_length > MAX_IMAGE_BYTES:
                raise ImageDownloadError("图片超过 Base64 输入大小限制")
        data = bytearray()
        while True:
            chunk = response.read(CHUNK_SIZE)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_IMAGE_BYTES:
                raise ImageDownloadError("图片超过 Base64 输入大小限制")
        return bytes(data)
