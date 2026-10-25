import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ops_api.app import create_app
from ops_api.daily_digest import INTERNAL_DIGEST_PATH, register_daily_digest_endpoint
from ops_api.settings import OpsSettings
from ops_api.supervisor import OpsActionError, OpsSupervisor

TOKEN = "a" * 64
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
PUBLIC_PATH = "/api/v1/nonebot/daily-digest"


def test_manual_endpoint_is_authenticated_and_defaults_to_preview():
    app = FastAPI()
    runner = AsyncMock(return_value={"status": "preview", "message": "日报"})
    register_daily_digest_endpoint(app, lambda: TOKEN, runner)
    client = TestClient(app)
    assert (
        client.post(INTERNAL_DIGEST_PATH, json={"group_id": "123"}).status_code == 401
    )
    assert (
        client.post(
            INTERNAL_DIGEST_PATH, headers=HEADERS, json={"group_id": "123"}
        ).json()["status"]
        == "preview"
    )
    runner.assert_awaited_once_with(group_id="123", send=False, force=False)
    assert (
        client.post(
            INTERNAL_DIGEST_PATH,
            headers=HEADERS,
            json={"group_id": "123", "send": "false"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            INTERNAL_DIGEST_PATH, headers=HEADERS, json={"group_id": "../bad"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            INTERNAL_DIGEST_PATH,
            headers=HEADERS,
            json={"group_id": "123", "path": "/etc/passwd"},
        ).status_code
        == 422
    )


def test_public_manual_route_requires_token_and_validates_send_flags(tmp_path):
    trigger = AsyncMock(return_value={"status": "sent", "message_id": "99"})
    app = create_app(
        OpsSettings(repo_root=tmp_path, api_token=TOKEN),
        SimpleNamespace(trigger_daily_digest=trigger),
    )
    client = TestClient(app)
    assert (
        client.post(PUBLIC_PATH, json={"group_id": "123", "send": True}).status_code
        == 401
    )
    trigger.assert_not_awaited()
    response = client.post(
        PUBLIC_PATH,
        headers=HEADERS,
        json={"group_id": "123", "send": True, "force": True},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "sent"
    trigger.assert_awaited_once_with({"group_id": "123", "send": True, "force": True})


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 404, 409, 503])
async def test_operations_proxy_forwards_to_loaded_nonebot_with_token(
    tmp_path, monkeypatch, status
):
    env = tmp_path / ".env"
    env.write_text("HOST=127.0.0.1\nPORT=2333\n")
    values = {"group_id": "123", "send": False, "force": False}

    def handle(request):
        assert str(request.url) == f"http://127.0.0.1:2333{INTERNAL_DIGEST_PATH}"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert json.loads(request.content) == values
        return httpx.Response(
            status,
            json={"status": "preview"} if status == 200 else {"detail": "not ready"},
        )

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        "ops_api.supervisor.httpx.AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(handle), **kwargs),
    )
    supervisor = OpsSupervisor(
        OpsSettings(repo_root=tmp_path, api_token=TOKEN, nonebot_env_file=env)
    )
    if status == 200:
        assert await supervisor.trigger_daily_digest(values) == {"status": "preview"}
    else:
        with pytest.raises(OpsActionError) as error:
            await supervisor.trigger_daily_digest(values)
        assert error.value.status_code == status
