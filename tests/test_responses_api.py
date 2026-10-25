import json
from types import SimpleNamespace

import httpx
import pytest
from multi_llm_chat.provider import LLMProvider, ProviderResponseError
from multi_llm_chat.responses_api import build_responses_request
from openai import AsyncOpenAI

from llm_usage import UsageStore


def test_responses_converts_images_tools_and_json_schema_without_mutating_messages():
    messages = [
        {"role": "system", "content": "test"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "图片"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,AA==", "detail": "low"},
                },
            ],
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call-1", "function": {"name": "number", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "42"},
    ]
    original = json.dumps(messages)
    schema = {"name": "result", "strict": True, "schema": {"type": "object"}}
    request = build_responses_request(
        {
            "messages": messages,
            "max_tokens": 100,
            "tools": [{"type": "web_search", "sources": ["doubao"]}],
            "tool_choice": {"type": "function", "function": {"name": "number"}},
            "response_format": {"type": "json_schema", "json_schema": schema},
        }
    )

    assert json.dumps(messages) == original
    assert request["input"][1]["content"] == [
        {"type": "input_text", "text": "图片"},
        {
            "type": "input_image",
            "image_url": "data:image/png;base64,AA==",
            "detail": "low",
        },
    ]
    assert request["input"][2:] == [
        {
            "type": "function_call",
            "call_id": "call-1",
            "name": "number",
            "arguments": "{}",
        },
        {"type": "function_call_output", "call_id": "call-1", "output": "42"},
    ]
    assert request["tool_choice"] == {"type": "function", "name": "number"}
    assert request["text"]["format"] == {"type": "json_schema", **schema}
    assert request["tools"] == [{"type": "web_search", "sources": ["doubao"]}]


def response(output, **overrides):
    return {
        "id": "resp-1",
        "object": "response",
        "created_at": 0,
        "model": "model",
        "status": "completed",
        "output": output,
        "usage": {
            "input_tokens": 12,
            "output_tokens": 4,
            "total_tokens": 16,
            "input_tokens_details": {"cached_tokens": 2},
            "output_tokens_details": {"reasoning_tokens": 1},
        },
        **overrides,
    }


def text_output(text):
    return {
        "id": "msg-1",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def make_provider(tmp_path, replies):
    requests = []

    def handle(request):
        assert request.url.path == "/api/v3/responses"
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, json=replies[len(requests) - 1], headers={"x-request-id": "ark-test"}
        )

    routes = {
        "providers": {
            "ark": {
                "api": "responses",
                "base_url": "https://example.com/api/v3",
                "api_key_field": "api_key",
                "supports_tools": True,
            }
        },
        "models": {"model": {"provider": "ark"}},
    }
    path = tmp_path / "routes.json"
    path.write_text(json.dumps(routes))
    usage = UsageStore(tmp_path / "usage.sqlite3")
    usage.initialize()
    logger = SimpleNamespace(
        info=lambda *a: None, warning=lambda *a: None, error=lambda *a: None
    )
    config = SimpleNamespace(
        llm_chat_model="model", llm_chat_temperature=0.7, llm_chat_max_tokens=1000
    )
    provider = LLMProvider(config, path, tmp_path / "logs", logger, usage)
    client = AsyncOpenAI(
        api_key="test",
        base_url="https://example.com/api/v3",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    provider._client_for = lambda _: client
    return provider, requests, client


@pytest.mark.asyncio
async def test_responses_sdk_tool_roundtrip_replays_reasoning_and_builtin_items(
    tmp_path,
):
    output = [
        {
            "id": "rs-1",
            "type": "reasoning",
            "summary": [],
            "encrypted_content": "opaque",
            "content": [{"type": "reasoning_text", "text": "private"}],
        },
        {
            "id": "ws-1",
            "type": "web_search_call",
            "status": "completed",
            "action": {"type": "search", "query": "weather"},
        },
        {
            "id": "fc-1",
            "type": "function_call",
            "status": "completed",
            "call_id": "call-1",
            "name": "number",
            "arguments": "{}",
        },
        {
            "id": "fc-2",
            "type": "function_call",
            "status": "completed",
            "call_id": "call-2",
            "name": "other",
            "arguments": "{}",
        },
    ]
    provider, requests, client = make_provider(
        tmp_path, [response(output), response([text_output("42")])]
    )
    messages = [{"role": "user", "content": "调用工具"}]
    try:
        turn = await provider.complete(messages, "direct_chat_agent", "t", 0)
        assert not turn.content
        assert [call.id for call in turn.tool_calls] == ["call-1", "call-2"]
        messages.extend(
            [
                turn.as_message(),
                {"role": "tool", "tool_call_id": "call-1", "content": "42"},
                {"role": "tool", "tool_call_id": "call-2", "content": "ok"},
            ]
        )
        final = await provider.complete(messages, "direct_chat_agent", "t", 1)
        assert final.content == "42"
        replay = requests[1]["input"][1:-2]
        assert [item["type"] for item in replay] == [item["type"] for item in output]
        assert replay[0]["encrypted_content"] == "opaque"
        assert replay[0]["content"] == output[0]["content"]
        assert requests[1]["input"][-2]["call_id"] == "call-1"
        assert "_responses_output" not in json.dumps(requests)
        totals = provider._usage_store.query()["totals"]
        assert totals["requests"] == 2
        assert totals["total_tokens"] == 32
        assert totals["cached_tokens"] == 4
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_responses_json_output_and_logs(tmp_path):
    provider, requests, client = make_provider(
        tmp_path, [response([text_output('{"ok":true}')])]
    )
    try:
        turn = await provider.complete(
            [{"role": "user", "content": "输出 JSON"}],
            "daily_digest",
            "t",
            0,
            response_format={"type": "json_object"},
        )
        assert json.loads(turn.content) == {"ok": True}
        assert requests[0]["text"]["format"] == {"type": "json_object"}
        records = [
            json.loads(line)
            for line in next((tmp_path / "logs").glob("*.jsonl"))
            .read_text()
            .splitlines()
        ]
        assert records[0]["metadata"]["api"] == "responses"
        assert records[1]["response"]["output"][0]["content"][0]["text"] == turn.content
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,details,output",
    [
        ("incomplete", {"reason": "max_output_tokens"}, [text_output("截断")]),
        ("failed", None, []),
        (
            "incomplete",
            {"reason": "max_output_tokens"},
            [
                {
                    "type": "function_call",
                    "call_id": "c",
                    "name": "number",
                    "arguments": "{}",
                }
            ],
        ),
    ],
)
async def test_responses_rejects_incomplete_output_even_for_json_and_tools(
    tmp_path, status, details, output
):
    provider, _, client = make_provider(
        tmp_path, [response(output, status=status, incomplete_details=details)]
    )
    try:
        with pytest.raises(ProviderResponseError, match="未正常结束"):
            await provider.complete(
                [{"role": "user", "content": "test"}], "memory_maintenance", "t", 0
            )
    finally:
        await client.close()
