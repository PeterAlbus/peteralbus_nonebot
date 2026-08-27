import json
from types import SimpleNamespace

import pytest
from multi_llm_chat.provider import (
    UPSTREAM_BLOCKED_CONTENT,
    UPSTREAM_BLOCKED_NOTICE,
    LLMProvider,
    ProviderResponseError,
    normalize_outbound_content,
)


class Logger:
    def __init__(self):
        self.info_records = []
        self.error_records = []
        self.warning_records = []

    def info(self, *args, **kwargs):
        self.info_records.append((args, kwargs))

    def error(self, *args, **kwargs):
        self.error_records.append((args, kwargs))

    def warning(self, *args, **kwargs):
        self.warning_records.append((args, kwargs))


def config():
    return SimpleNamespace(
        llm_chat_model="model",
        llm_chat_temperature=0.7,
        llm_chat_max_tokens=1000,
        llm_chat_request_timeout=10,
        llm_chat_image_understanding=None,
        api_key="key",
    )


def test_provider_merges_builtin_and_custom_tools_only_for_agent_request(tmp_path):
    routes = {
        "providers": {
            "provider": {
                "base_url": "https://example.com/v1",
                "api_key_field": "api_key",
                "supports_tools": True,
                "create_params": {"tools": [{"type": "web_search"}]},
            }
        },
        "models": {"model": {"provider": "provider"}},
    }
    routes_path = tmp_path / "routes.json"
    routes_path.write_text(json.dumps(routes), encoding="utf-8")
    provider = LLMProvider(config(), routes_path, tmp_path / "logs", Logger())
    custom = {"type": "function", "function": {"name": "add"}}

    with_builtin = provider._build_request(
        "model",
        routes["providers"]["provider"],
        routes["models"]["model"],
        [{"role": "user", "content": "x"}],
        [custom],
        "auto",
        None,
        True,
    )
    without_builtin = provider._build_request(
        "model",
        routes["providers"]["provider"],
        routes["models"]["model"],
        [{"role": "user", "content": "x"}],
        None,
        None,
        {"type": "json_object"},
        False,
    )

    assert with_builtin["tools"] == [{"type": "web_search"}, custom]
    assert "tools" not in without_builtin
    assert without_builtin["response_format"] == {"type": "json_object"}


def test_provider_uses_provider_specific_output_token_field(tmp_path):
    routes = {
        "providers": {
            "provider": {
                "base_url": "https://example.com/v1",
                "api_key_field": "api_key",
                "supports_tools": True,
                "max_tokens_field": "max_completion_tokens",
            }
        },
        "models": {"model": {"provider": "provider"}},
    }
    routes_path = tmp_path / "routes.json"
    routes_path.write_text(json.dumps(routes), encoding="utf-8")
    provider = LLMProvider(config(), routes_path, tmp_path / "logs", Logger())

    request = provider._build_request(
        "model",
        routes["providers"]["provider"],
        routes["models"]["model"],
        [{"role": "user", "content": "x"}],
        None,
        None,
        None,
        False,
    )

    assert request["max_completion_tokens"] == 1000
    assert "max_tokens" not in request


def test_image_understanding_uses_model_default_and_env_override(tmp_path):
    routes = {
        "providers": {
            "provider": {
                "base_url": "https://example.com/v1",
                "api_key_field": "api_key",
                "supports_tools": True,
            }
        },
        "models": {
            "mimo-v2.5": {
                "provider": "provider",
                "supports_image_understanding": True,
            },
            "deepseek-v4-flash": {
                "provider": "provider",
                "supports_image_understanding": False,
            },
        },
    }
    routes_path = tmp_path / "routes.json"
    routes_path.write_text(json.dumps(routes), encoding="utf-8")

    mimo_config = config()
    mimo_config.llm_chat_model = "mimo-v2.5"
    mimo = LLMProvider(mimo_config, routes_path, tmp_path / "logs", Logger())
    deepseek_config = config()
    deepseek_config.llm_chat_model = "deepseek-v4-flash"
    deepseek = LLMProvider(
        deepseek_config,
        routes_path,
        tmp_path / "logs",
        Logger(),
    )

    assert mimo.image_understanding_enabled() is True
    assert deepseek.image_understanding_enabled() is False

    mimo_config.llm_chat_image_understanding = False
    deepseek_config.llm_chat_image_understanding = True

    assert mimo.image_understanding_enabled() is False
    assert deepseek.image_understanding_enabled() is True


class FakeRawResponse:
    def __init__(self, content, finish_reason="stop", usage=None):
        usage = usage or {
            "prompt_tokens": 12,
            "completion_tokens": 4,
            "total_tokens": 16,
        }
        self.status_code = 200
        self.request_id = "upstream-request-1"
        self.retries_taken = 0
        self.headers = {
            "x-request-id": self.request_id,
            "x-ratelimit-remaining-requests": "99",
            "set-cookie": "must-not-be-recorded",
        }
        self.text = json.dumps(
            {
                "id": "completion-1",
                "model": "model",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": finish_reason,
                    }
                ],
                "usage": usage,
            },
            ensure_ascii=False,
        )
        self._result = SimpleNamespace(
            _request_id=self.request_id,
            choices=[
                SimpleNamespace(
                    finish_reason=finish_reason,
                    message=SimpleNamespace(
                        content=content,
                        tool_calls=None,
                        reasoning_content="",
                    ),
                )
            ],
            usage=SimpleNamespace(model_dump=lambda: usage),
        )

    def parse(self):
        return self._result


class FakeCreate:
    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.call_count = 0

    async def create(self, **kwargs):
        self.call_count += 1
        if self.error is not None:
            raise self.error
        return self.responses[min(self.call_count - 1, len(self.responses) - 1)]


def fake_client(response=None, responses=None, error=None):
    configured_responses = responses or ([response] if response is not None else [])
    return SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                with_raw_response=FakeCreate(
                    responses=configured_responses,
                    error=error,
                )
            )
        )
    )


def provider_for_completion(tmp_path, logger=None):
    routes = {
        "providers": {
            "provider": {
                "base_url": "https://example.com/v1",
                "api_key_field": "api_key",
                "supports_tools": True,
            }
        },
        "models": {"model": {"provider": "provider"}},
    }
    routes_path = tmp_path / "routes.json"
    routes_path.write_text(json.dumps(routes), encoding="utf-8")
    return LLMProvider(config(), routes_path, tmp_path / "logs", logger or Logger())


@pytest.mark.asyncio
async def test_provider_records_raw_response_and_translates_exact_upstream_block(
    tmp_path,
):
    logger = Logger()
    provider = provider_for_completion(tmp_path, logger)
    provider._client_for = lambda provider_config: fake_client(
        FakeRawResponse(
            UPSTREAM_BLOCKED_CONTENT,
            finish_reason="content_filter",
            usage={"input_tokens": 0, "output_tokens": 0},
        )
    )

    turn = await provider.complete(
        messages=[{"role": "user", "content": "测试"}],
        request_type="direct_chat_agent",
        turn_id="turn-1",
        step=0,
    )

    records = [
        json.loads(line)
        for line in next((tmp_path / "logs").glob("llm-requests-*.jsonl"))
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert turn.content == UPSTREAM_BLOCKED_NOTICE
    assert [record["kind"] for record in records] == [
        "request",
        "response",
        "request",
        "response",
        "request",
        "response",
        "request",
        "response",
    ]
    assert records[0]["metadata"]["model"] == "model"
    assert [records[index]["metadata"]["attempt"] for index in (1, 3, 5, 7)] == [
        1,
        2,
        3,
        4,
    ]
    assert (
        records[1]["response"]["choices"][0]["message"]["content"]
        == UPSTREAM_BLOCKED_CONTENT
    )
    assert records[1]["metadata"]["provider_request_id"] == "upstream-request-1"
    assert records[1]["metadata"]["sdk_retries_taken"] == 0
    assert "set-cookie" not in records[1]["metadata"]["response_headers"]
    assert records[1]["handling"] == {
        "upstream_blocked": True,
        "retry_scheduled": True,
        "embedded_reasoning_removed": False,
        "output_rejection": None,
        "outbound_content": None,
    }
    assert records[7]["handling"] == {
        "upstream_blocked": True,
        "retry_scheduled": False,
        "embedded_reasoning_removed": False,
        "output_rejection": None,
        "outbound_content": UPSTREAM_BLOCKED_NOTICE,
    }
    assert logger.info_records
    assert len(logger.warning_records) == 3


@pytest.mark.asyncio
async def test_provider_returns_first_non_blocked_retry(tmp_path):
    provider = provider_for_completion(tmp_path)
    blocked = FakeRawResponse(
        UPSTREAM_BLOCKED_CONTENT,
        finish_reason="content_filter",
        usage={"input_tokens": 0, "output_tokens": 0},
    )
    client = fake_client(responses=[blocked, blocked, FakeRawResponse("恢复后的回复")])
    provider._client_for = lambda provider_config: client

    turn = await provider.complete(
        messages=[{"role": "user", "content": "测试"}],
        request_type="direct_chat_agent",
        turn_id="turn-recovered",
        step=0,
    )

    assert turn.content == "恢复后的回复"
    assert client.chat.completions.with_raw_response.call_count == 3


@pytest.mark.asyncio
async def test_provider_only_translates_exact_upstream_block_content(tmp_path):
    provider = provider_for_completion(tmp_path)
    content = f" {UPSTREAM_BLOCKED_CONTENT} "
    client = fake_client(FakeRawResponse(content, finish_reason="content_filter"))
    provider._client_for = lambda provider_config: client

    turn = await provider.complete(
        messages=[{"role": "user", "content": "测试"}],
        request_type="direct_chat_agent",
        turn_id="turn-2",
        step=0,
    )

    assert turn.content == UPSTREAM_BLOCKED_CONTENT
    assert client.chat.completions.with_raw_response.call_count == 1


@pytest.mark.asyncio
async def test_provider_records_structured_upstream_error(tmp_path):
    class UpstreamError(RuntimeError):
        status_code = 429
        request_id = "upstream-error-1"

        def __init__(self):
            super().__init__()
            self.body = {"error": {"code": "rate_limit", "message": "slow down"}}

    logger = Logger()
    provider = provider_for_completion(tmp_path, logger)
    provider._client_for = lambda provider_config: fake_client(error=UpstreamError())

    with pytest.raises(UpstreamError):
        await provider.complete(
            messages=[{"role": "user", "content": "测试"}],
            request_type="direct_chat_agent",
            turn_id="turn-3",
            step=0,
        )

    records = [
        json.loads(line)
        for line in next((tmp_path / "logs").glob("llm-requests-*.jsonl"))
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [record["kind"] for record in records] == ["request", "error"]
    assert records[1]["metadata"]["status_code"] == 429
    assert records[1]["metadata"]["provider_request_id"] == "upstream-error-1"
    assert records[1]["error"]["body"]["error"]["code"] == "rate_limit"
    assert logger.error_records


def test_outbound_content_removes_embedded_reasoning_before_visible_reply():
    content, removed = normalize_outbound_content(
        "I should identify the speaker first.\n</think>\n确实认错人了。"
    )

    assert content == "确实认错人了。"
    assert removed is True


def test_outbound_content_rejects_internal_event_context():
    with pytest.raises(ProviderResponseError, match="内部群聊消息上下文"):
        normalize_outbound_content(
            '内部群聊消息上下文（仅用于理解，不得复述）：\nevent_id="e1"'
        )
