import hashlib
import json
from datetime import date, datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from llm_accounts import (
    AccountQueryError,
    sign_volc_request,
)
from ops_api.app import create_app
from ops_api.provider_accounts import provider_accounts_for_settings
from ops_api.settings import OpsSettings

TOKEN = "a" * 64
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def settings(tmp_path):
    env = tmp_path / ".env"
    env.write_text("LLM_CHAT_DEEPSEEK_API_KEY=secret-ds\n")
    return OpsSettings(
        repo_root=tmp_path,
        api_token=TOKEN,
        nonebot_env_file=env,
        volc_access_key="access-key",
        volc_secret_key="secret-key",
    )


@pytest.mark.asyncio
async def test_balances_dispatch_and_report_actual_scopes(settings):
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.host == "api.deepseek.com":
            assert request.url.path == "/user/balance"
            assert request.headers["authorization"] == "Bearer secret-ds"
            return httpx.Response(
                200,
                json={
                    "is_available": True,
                    "balance_infos": [
                        {
                            "currency": "CNY",
                            "total_balance": "12.3",
                            "granted_balance": "2",
                            "topped_up_balance": "10.3",
                        }
                    ],
                },
            )
        assert request.url.host == "billing.volcengineapi.com"
        assert request.url.params["Action"] == "QueryBalanceAcct"
        assert "/billing/request" in request.headers["authorization"]
        return httpx.Response(
            200, json={"Result": {"AvailableBalance": "15", "CashBalance": "15"}}
        )

    accounts = provider_accounts_for_settings(settings, httpx.MockTransport(handle))
    result = await accounts.balances()
    entries = {x["provider"]: x for x in result["providers"]}
    assert entries["deepseek"]["balance"]["balance_infos"][0]["total_balance"] == "12.3"
    assert entries["volc_ark"]["scope"] == "account"
    assert entries["xiaomi_mimo"]["status"] == "unsupported"
    assert len(calls) == 2
    assert "secret" not in json.dumps(result)


@pytest.mark.asyncio
async def test_volc_usage_exact_request_filters_and_date_range(settings):
    def handle(request):
        assert request.method == "POST"
        assert request.url.host == "ark.cn-beijing.volcengineapi.com"
        assert request.url.params["Action"] == "GetInferenceUsage"
        assert (
            request.headers["x-content-sha256"]
            == hashlib.sha256(request.content).hexdigest()
        )
        assert json.loads(request.content) == {
            "StartTime": "2026-10-01",
            "EndTime": "2026-10-22",
            "QueryInterval": "Day",
            "ProjectName": "default",
            "Filters": [
                {"Key": "ApikeyID", "Values": ["apikey-resource"]},
                {"Key": "ModelName", "Values": ["model"]},
            ],
        }
        return httpx.Response(
            200,
            json={
                "Result": {
                    "Fields": [{"Name": "TotalTokens", "Type": "BIGINT"}],
                    "Data": [["150"]],
                    "DataCount": 1,
                }
            },
        )

    accounts = provider_accounts_for_settings(settings, httpx.MockTransport(handle))
    data = await accounts.volc_usage(
        date(2026, 10, 1), date(2026, 10, 22), "apikey-resource", "model"
    )
    assert data["scope"] == "api_key"
    assert data["usage"]["Data"] == [["150"]]
    with pytest.raises(ValueError):
        await accounts.volc_usage(date(2026, 10, 22), date(2026, 10, 1), None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["http", "timeout", "error_metadata", "malformed"])
async def test_upstream_failures_do_not_expose_credentials(settings, failure):
    def handle(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("secret-ds", request=request)
        if failure == "http":
            return httpx.Response(401, text="secret-ds")
        if failure == "error_metadata":
            return httpx.Response(
                200, json={"ResponseMetadata": {"Error": {"Message": "secret-ds"}}}
            )
        return httpx.Response(200, text="invalid secret-ds")

    data = await provider_accounts_for_settings(
        settings, httpx.MockTransport(handle)
    ).balances()
    assert "secret-ds" not in json.dumps(data)
    assert all(x["status"] != "ok" for x in data["providers"])


@pytest.mark.asyncio
async def test_unconfigured_credentials_are_not_queried(tmp_path):
    settings = OpsSettings(
        repo_root=tmp_path, api_token=TOKEN, nonebot_env_file=tmp_path / ".env"
    )

    def handle(request):
        raise AssertionError("network should not be called")

    accounts = provider_accounts_for_settings(settings, httpx.MockTransport(handle))
    data = await accounts.balances()
    assert [x["status"] for x in data["providers"]] == [
        "not_configured",
        "not_configured",
        "unsupported",
    ]
    with pytest.raises(AccountQueryError, match="not_configured"):
        await accounts.volc_usage(date(2026, 10, 22), date(2026, 10, 22), None, None)


def test_signing_is_deterministic_and_includes_body():
    args = (
        "POST",
        "ark.cn-beijing.volcengineapi.com",
        "GetInferenceUsage",
        "2024-01-01",
        "ark",
        "ak",
        "sk",
    )
    now = datetime(2026, 10, 22, 0, tzinfo=timezone.utc)
    url, headers = sign_volc_request(*args, body=b"{}", now=now)
    assert sign_volc_request(*args, body=b"{}", now=now) == (url, headers)
    assert headers["x-date"] == "20261022T000000Z"
    assert "Credential=ak/20261022/cn-beijing/ark/request" in headers["Authorization"]
    assert (
        "SignedHeaders=content-type;host;x-content-sha256;x-date"
        in headers["Authorization"]
    )
    assert (
        sign_volc_request(*args, body=b'{"x":1}', now=now)[1]["Authorization"]
        != headers["Authorization"]
    )


def test_api_auth_dates_and_error_status(settings):
    app = create_app(settings)
    app.state.provider_accounts_factory = lambda: provider_accounts_for_settings(
        OpsSettings(
            repo_root=settings.repo_root,
            api_token=TOKEN,
            nonebot_env_file=settings.nonebot_env_file.parent / "missing",
        )
    )
    client = TestClient(app)
    urls = ["/api/v1/llm/balances", "/api/v1/llm/providers/volc_ark/usage"]
    for url in urls:
        assert client.get(url).status_code == 401
    assert client.get(urls[0], headers=HEADERS).status_code == 200
    assert client.get(urls[1], headers=HEADERS).status_code == 422
    response = client.get(
        urls[1],
        headers=HEADERS,
        params={"start_date": "2026-10-01", "end_date": "2026-10-22"},
    )
    assert response.status_code == 503
