import json
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from multi_llm_chat.tools import ToolContext, build_default_tool_registry

from llm_usage import UsageStore, normalize_usage
from ops_api.app import create_app
from ops_api.settings import OpsSettings


@pytest.fixture
def store(tmp_path):
    store = UsageStore(tmp_path / "usage.sqlite3")
    store.initialize()
    return store


def record(
    store, key, at, usage, provider="deepseek", model="flash", kind="chat", attempt=1
):
    store.begin(key, attempt, provider, model, kind, at)
    store.finish(key, attempt, usage, "completed")


def test_normalize_usage_does_not_double_count_subsets():
    assert normalize_usage(
        {
            "prompt_tokens": 100,
            "completion_tokens": 40,
            "prompt_cache_hit_tokens": 80,
            "completion_tokens_details": {"reasoning_tokens": 30},
        }
    ) == {
        "input_tokens": 100,
        "output_tokens": 40,
        "total_tokens": 140,
        "cached_tokens": 80,
        "reasoning_tokens": 30,
    }
    assert (
        normalize_usage({"input_tokens": True, "output_tokens": -1})["total_tokens"]
        is None
    )


def test_query_beijing_dates_filters_retries_and_persistence(store):
    usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
    record(store, "before", "2026-10-21T15:59:59+00:00", usage)
    record(store, "same", "2026-10-21T16:00:00+00:00", usage)
    record(store, "same", "2026-10-22T01:00:00+00:00", usage, attempt=2)
    record(
        store,
        "other",
        "2026-10-22T02:00:00+00:00",
        usage,
        provider="volc_ark",
        kind="daily_digest",
    )
    record(store, "after", "2026-10-22T16:00:00+00:00", usage)
    start = store.query(date(2026, 10, 22), date(2026, 10, 22))["tracking_started_at"]
    reopened = UsageStore(store.path)
    reopened.initialize()
    report = reopened.query(date(2026, 10, 22), date(2026, 10, 22), provider="deepseek")
    assert report["totals"]["total_tokens"] == 240
    assert report["totals"]["requests"] == 2
    assert report["tracking_started_at"] == start
    assert len(report["breakdown"]) == 1
    assert report["breakdown"][0]["date"] == "2026-10-22"
    assert (
        reopened.query(
            date(2026, 10, 22), date(2026, 10, 22), request_type="daily_digest"
        )["totals"]["requests"]
        == 1
    )
    assert (
        reopened.query(date(2026, 10, 22), date(2026, 10, 22), model="missing")[
            "totals"
        ]["requests"]
        == 0
    )


def test_unknown_pending_and_sdk_retries_are_explicit(store):
    store.begin("error", 1, "mimo", "flash", "chat")
    store.finish("error", 1, {}, "error")
    store.begin("pending", 1, "mimo", "flash", "chat")
    store.begin("known", 1, "mimo", "flash", "chat")
    store.finish("known", 1, {"input_tokens": 10, "output_tokens": 5}, "completed", 2)
    summary = store.query()["totals"]
    assert summary["requests"] == 3
    assert summary["total_tokens"] == 15
    assert summary["unknown_usage_requests"] == 2
    assert summary["pending_requests"] == 1
    assert summary["failed_requests"] == 1
    assert summary["sdk_retries_without_usage"] == 2
    assert summary["cached_tokens"] is None


def test_ops_usage_auth_query_validation_and_missing_db(store, tmp_path):
    token = "a" * 64
    headers = {"Authorization": f"Bearer {token}"}
    settings = OpsSettings(
        repo_root=tmp_path, api_token=token, llm_usage_db_file=store.path
    )
    client = TestClient(create_app(settings))
    url = "/api/v1/llm/usage"
    store.begin("one", 1, "mimo", "flash", "chat")
    store.finish("one", 1, {"input_tokens": 2, "output_tokens": 3}, "completed")
    assert client.get(url).status_code == 401
    response = client.get(url, headers=headers)
    assert response.status_code == 200
    assert response.json()["totals"]["total_tokens"] == 5
    assert (
        client.get(url, headers=headers, params={"start_date": "bad"}).status_code
        == 422
    )
    assert (
        client.get(
            url,
            headers=headers,
            params={"start_date": "2026-10-23", "end_date": "2026-10-22"},
        ).status_code
        == 422
    )
    missing = tmp_path / "missing.db"
    client = TestClient(
        create_app(
            OpsSettings(repo_root=tmp_path, api_token=token, llm_usage_db_file=missing)
        )
    )
    assert client.get(url, headers=headers).status_code == 503
    assert not missing.exists()


@pytest.mark.asyncio
async def test_usage_tool_matches_store_and_no_account_credentials(store, tmp_path):
    store.begin("one", 1, "volc_ark", "lite", "daily_digest")
    store.finish("one", 1, {"input_tokens": 40, "output_tokens": 10}, "completed")
    registry = build_default_tool_registry(store, object(), object(), object(), 10, 12000)
    context = ToolContext("turn", "group", "user", SimpleNamespace(), tmp_path)
    result = json.loads(await registry.execute("get_token_usage", "{}", context))
    assert result["success"] is True
    assert result["data"]["totals"] == store.query()["totals"]
    invalid = json.loads(
        await registry.execute("get_token_usage", '{"start_date":"bad"}', context)
    )
    assert invalid["success"] is False
