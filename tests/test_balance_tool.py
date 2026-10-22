import json
from functools import partial
from types import SimpleNamespace

import httpx
import pytest
from multi_llm_chat.provider import LLMProvider
from multi_llm_chat.tools import ToolContext, build_default_tool_registry

from llm_accounts import ProviderAccounts
from llm_usage import UsageStore


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_name", ["deepseek", "volc_ark", "xiaomi_mimo"])
async def test_balance_tool_uses_loaded_provider_and_matching_credentials(
    tmp_path, monkeypatch, provider_name
):
    routes_path = tmp_path / "routes.json"
    routes = {
        "providers": {
            provider_name: {"api_key_field": "active_key", "supports_tools": True}
        },
        "models": {"arbitrary-model-name": {"provider": provider_name}},
    }
    routes_path.write_text(json.dumps(routes))
    config = SimpleNamespace(
        llm_chat_model="arbitrary-model-name",
        active_key="active-key",
        llm_chat_deepseek_api_key="different-key",
    )
    store = UsageStore(tmp_path / "usage.sqlite3")
    store.initialize()
    provider = LLMProvider(config, routes_path, tmp_path / "logs", object(), store)
    # The running bot continues using its loaded routes until restarted.
    routes["models"]["arbitrary-model-name"]["provider"] = "other"
    routes_path.write_text(json.dumps(routes))
    calls = []

    def handle(request):
        calls.append(request)
        if provider_name == "deepseek":
            assert request.url.host == "api.deepseek.com"
            assert request.headers["Authorization"] == "Bearer active-key"
            return httpx.Response(
                200,
                json={
                    "is_available": True,
                    "balance_infos": [
                        {
                            "currency": "CNY",
                            "total_balance": "12",
                            "granted_balance": "2",
                            "topped_up_balance": "10",
                        }
                    ],
                },
            )
        assert provider_name == "volc_ark"
        assert request.url.host == "billing.volcengineapi.com"
        assert "Credential=billing-ak/" in request.headers["Authorization"]
        return httpx.Response(200, json={"Result": {"AvailableBalance": "15"}})

    monkeypatch.setattr(
        "multi_llm_chat.provider.ProviderAccounts",
        partial(ProviderAccounts, transport=httpx.MockTransport(handle)),
    )
    registry = build_default_tool_registry(
        usage_store=store,
        balance_query=partial(provider.get_balance, "billing-ak", "billing-sk"),
        roster_service=object(),
        memory_store=object(),
        tool_timeout_seconds=20,
        output_max_chars=12000,
    )
    context = ToolContext("turn", "group", "user", object(), tmp_path)
    response = json.loads(await registry.execute("get_balance", "{}", context))
    assert response["success"] is True
    data = response["data"]
    assert provider.current_provider_name() == provider_name
    assert data["provider"] == provider_name
    assert data["model"] == "arbitrary-model-name"
    if provider_name == "xiaomi_mimo":
        assert data["status"] == "unsupported"
        assert calls == []
    else:
        assert data["status"] == "ok"
        assert data["scope"] == "account"
        assert len(calls) == 1
    assert "active-key" not in json.dumps(data)
    assert "billing-sk" not in json.dumps(data)
    rejected = json.loads(
        await registry.execute("get_balance", '{"provider":"deepseek"}', context)
    )
    assert rejected["success"] is False


@pytest.mark.asyncio
async def test_current_balance_reports_missing_billing_credentials(tmp_path):
    path = tmp_path / "routes.json"
    path.write_text(
        json.dumps(
            {
                "providers": {"volc_ark": {"supports_tools": True}},
                "models": {"deepseek-on-ark": {"provider": "volc_ark"}},
            }
        )
    )
    store = UsageStore(tmp_path / "usage.sqlite3")
    provider = LLMProvider(
        SimpleNamespace(llm_chat_model="deepseek-on-ark"),
        path,
        tmp_path / "logs",
        object(),
        store,
    )
    result = await provider.get_balance("", "")
    assert result == {
        "model": "deepseek-on-ark",
        "provider": "volc_ark",
        "status": "not_configured",
    }
