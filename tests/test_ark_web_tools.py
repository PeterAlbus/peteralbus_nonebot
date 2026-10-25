import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from multi_llm_chat.provider import LLMProvider, ProviderResponseError
from multi_llm_chat.tools import ToolContext, build_default_tool_registry
from openai import AsyncOpenAI

from llm_usage import UsageStore


def registry(url_reader=None):
    return build_default_tool_registry(
        usage_store=object(),
        balance_query=AsyncMock(),
        roster_service=object(),
        memory_store=object(),
        tool_timeout_seconds=2,
        output_max_chars=12000,
        url_reader=url_reader,
    )


@pytest.mark.asyncio
async def test_url_reader_registration_and_execution(tmp_path):
    reader = AsyncMock(
        return_value={"pages": [{"title": "Example", "content": "body"}]}
    )
    tools = registry(reader)
    schema = next(
        tool["function"]
        for tool in tools.schemas()
        if tool["function"]["name"] == "read_urls"
    )
    assert schema["strict"] is True
    assert schema["parameters"]["additionalProperties"] is False
    assert schema["parameters"]["required"] == ["url_list"]
    result = json.loads(
        await tools.execute(
            "read_urls",
            '{"url_list":["https://example.com"]}',
            ToolContext("turn", "group", "user", object(), tmp_path),
        )
    )
    assert result["success"] is True
    assert result["data"]["pages"][0]["content"] == "body"
    reader.assert_awaited_once_with(["https://example.com"])
    assert "read_urls" not in [
        tool["function"]["name"] for tool in registry().schemas()
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "urls",
    [
        [],
        ["https://example.com"] * 4,
        ["file:///etc/passwd"],
        ["https://user:secret@example.com"],
        ["http://127.0.0.1"],
        ["http://localhost"],
        ["http://[::1]"],
        ["not-a-url"],
    ],
)
async def test_url_reader_rejects_invalid_arguments_before_calling_api(tmp_path, urls):
    reader = AsyncMock()
    result = json.loads(
        await registry(reader).execute(
            "read_urls",
            json.dumps({"url_list": urls}),
            ToolContext("turn", "group", "user", object(), tmp_path),
        )
    )
    assert result["success"] is False
    reader.assert_not_awaited()


def provider_with_client(tmp_path, payload, status_code=200):
    routes = {
        "providers": {"volc_ark": {"supports_tools": True}},
        "models": {"model": {"provider": "volc_ark"}},
    }
    path = tmp_path / "routes.json"
    path.write_text(json.dumps(routes))
    config = SimpleNamespace(
        llm_chat_model="model", llm_chat_tool_output_max_chars=4000
    )
    provider = LLMProvider(
        config,
        path,
        tmp_path / "logs",
        object(),
        UsageStore(tmp_path / "usage.sqlite3"),
    )

    def handle(request):
        assert request.url == "https://ark.example/api/v3/tools/execute"
        assert request.headers["Authorization"] == "Bearer test-key"
        assert json.loads(request.content) == {
            "action_name": "LinkReader",
            "tool_name": "LinkReader",
            "parameters": {"url_list": ["https://one.example", "https://two.example"]},
        }
        return httpx.Response(status_code, json=payload)

    client = AsyncOpenAI(
        api_key="test-key",
        base_url="https://ark.example/api/v3",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    provider._client_for = lambda _: client
    return provider, client


@pytest.mark.asyncio
async def test_link_reader_sdk_response_handles_truncation_and_individual_page_failure(
    tmp_path,
):
    provider, client = provider_with_client(
        tmp_path,
        {
            "status_code": 200,
            "data": {
                "ark_web_data_list": [
                    {
                        "url": "https://one.example",
                        "title": "Title",
                        "content": "a" * 5000,
                        "error_code": "",
                    },
                    {
                        "url": "https://two.example",
                        "title": "",
                        "content": "should not use",
                        "error_code": "403",
                        "error_message": "page unavailable",
                    },
                ]
            },
        },
    )
    try:
        pages = (
            await provider.read_urls(["https://one.example", "https://two.example"])
        )["pages"]
        assert pages[0]["success"] is True
        assert pages[0]["truncated"] is True
        assert len(pages[0]["content"]) == 1000
        assert pages[1]["success"] is False
        assert pages[1]["content"] == ""
        assert pages[1]["error"] == "page unavailable"
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"status_code": 403, "data": {}},
        {"status_code": 200, "data": {}},
        {"status_code": 200, "data": {"ark_web_data_list": []}},
        {"status_code": 200, "data": {"ark_web_data_list": ["invalid", "invalid"]}},
    ],
)
async def test_link_reader_rejects_failed_or_malformed_upstream_response(
    tmp_path, payload
):
    provider, client = provider_with_client(tmp_path, payload)
    try:
        with pytest.raises(ProviderResponseError):
            await provider.read_urls(["https://one.example", "https://two.example"])
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_link_reader_reports_provider_security_rejection_to_the_model(tmp_path):
    provider, client = provider_with_client(
        tmp_path,
        {
            "error": {
                "code": "LinkReaderBaseError",
                "message": "link hit security strategy",
            },
        },
        status_code=500,
    )
    try:
        result = json.loads(
            await registry(provider.read_urls).execute(
                "read_urls",
                '{"url_list":["https://one.example","https://two.example"]}',
                ToolContext("turn", "group", "user", object(), tmp_path),
            )
        )
        assert result["success"] is False
        assert "安全策略拦截" in result["error"]
        assert "未获取正文" in result["error"]
    finally:
        await client.close()
