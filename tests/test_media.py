from datetime import datetime, timezone

import pytest
from multi_llm_chat.media import ImageDownloadError, ImageStore
from multi_llm_chat.models import ChatEvent

PNG_DATA = b"\x89PNG\r\n\x1a\n" + b"test-image-content"


def event_with_image(resource):
    return ChatEvent(
        event_id="image-event",
        group_id="100",
        role="user",
        source="onebot",
        user_id="200",
        content="前[图片]后",
        images=[resource],
        sent_at=datetime(2026, 8, 22, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_image_store_preserves_text_order_and_builds_only_selected_image(
    tmp_path,
):
    store = ImageStore(tmp_path)
    resource = await store.store_bytes(
        PNG_DATA,
        placeholder="[图片]",
        content_offset=1,
        media_namespace="onebot:100:image-event",
    )
    event = event_with_image(resource)

    text_content = await store.build_content(event, image_indices=[])
    selected_content = await store.build_content(event, image_indices=[0])

    assert text_content == "前[图片]后"
    assert selected_content[0] == {"type": "text", "text": "前"}
    assert selected_content[1]["type"] == "image_url"
    assert selected_content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert selected_content[2] == {"type": "text", "text": "后"}
    assert (tmp_path / "media" / resource.media_key).read_bytes() == PNG_DATA


@pytest.mark.asyncio
async def test_image_store_rejects_non_image_content(tmp_path):
    store = ImageStore(tmp_path)

    with pytest.raises(ImageDownloadError, match="格式不受支持"):
        await store.store_bytes(
            b"not an image",
            placeholder="[图片]",
            content_offset=0,
            media_namespace="onebot:100:invalid",
        )


@pytest.mark.asyncio
async def test_compressed_image_is_deleted_without_affecting_other_event(
    tmp_path,
):
    store = ImageStore(tmp_path)
    compressed_resource = await store.store_bytes(
        PNG_DATA,
        placeholder="[图片]",
        content_offset=1,
        media_namespace="onebot:100:image-event",
    )
    retained_resource = await store.store_bytes(
        PNG_DATA,
        placeholder="[图片]",
        content_offset=1,
        media_namespace="onebot:200:retained-event",
    )
    compressed = event_with_image(compressed_resource)
    retained = event_with_image(retained_resource).model_copy(
        update={"event_id": "retained", "group_id": "200"}
    )
    compressed_path = tmp_path / "media" / compressed_resource.media_key
    retained_path = tmp_path / "media" / retained_resource.media_key

    assert await store.delete_compressed_images([compressed], [retained]) == 1
    assert not compressed_path.exists()
    assert retained_path.is_file()
