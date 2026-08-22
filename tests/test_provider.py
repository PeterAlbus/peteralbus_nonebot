import json
from types import SimpleNamespace

from multi_llm_chat.provider import LLMProvider


class Logger:
    def info(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


def config():
    return SimpleNamespace(
        llm_chat_model="model",
        llm_chat_temperature=0.7,
        llm_chat_max_tokens=1000,
        llm_chat_request_timeout=10,
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
