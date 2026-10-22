from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from ops_api.app import create_app
from ops_api.settings import OpsSettings

TOKEN = "a" * 64
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
URL = "/api/v1/nonebot/model-routes/models"


@pytest.fixture
def routes_api(tmp_path):
    path = tmp_path / "routes.json"
    document = {
        "providers": {
            "ark": {"base_url": "https://example.com", "supports_tools": True}
        },
        "models": {"old": {"provider": "ark"}},
        "metadata": {"version": 1},
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o640)
    settings = OpsSettings(repo_root=tmp_path, api_token=TOKEN, model_routes_file=path)
    return TestClient(create_app(settings)), path, document


def test_read_models_returns_current_file_and_can_be_used_for_put(routes_api):
    client, path, document = routes_api
    original = path.read_bytes()
    response = client.get(URL, headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {"models": document["models"]}
    assert response.headers["Cache-Control"] == "no-store"
    assert path.read_bytes() == original
    assert client.put(URL, headers=HEADERS, json=response.json()).status_code == 200
    document["models"] = {
        "new": {"provider": "ark", "create_params": {"temperature": 0.5}}
    }
    path.write_text(json.dumps(document))
    assert client.get(URL, headers=HEADERS).json() == {"models": document["models"]}


def test_replace_models_preserves_providers_and_metadata(routes_api):
    client, path, original = routes_api
    models = {
        "new": {
            "provider": "ark",
            "supports_image_understanding": True,
            "create_params": {"extra_body": {"thinking": {"type": "disabled"}}},
        }
    }
    response = client.put(URL, headers=HEADERS, json={"models": models})
    assert response.status_code == 200
    assert response.json() == {"models": models, "restart_required": True}
    assert client.get(URL, headers=HEADERS).json() == {"models": models}
    assert json.loads(path.read_text()) == {**original, "models": models}
    assert path.stat().st_mode & 0o777 == 0o640
    assert client.put(URL, headers=HEADERS, json={"models": {}}).status_code == 200
    assert json.loads(path.read_text())["models"] == {}
    assert client.get(URL, headers=HEADERS).json() == {"models": {}}


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"models": []},
        {"models": {}, "providers": {}},
        {"models": {}, "path": "elsewhere.json"},
        {"models": {"new": {"provider": "unknown"}}},
        {"models": {"new": {"provider": "ark", "base_url": "https://other.example"}}},
        {
            "models": {
                "new": {"provider": "ark", "supports_image_understanding": "true"}
            }
        },
        {"models": {"new": {"provider": "ark", "create_params": []}}},
        {"models": {"": {"provider": "ark"}}},
        {"models": {"new": None}},
    ],
)
def test_invalid_updates_leave_file_unchanged(routes_api, payload):
    client, path, _ = routes_api
    original = path.read_bytes()
    assert client.put(URL, headers=HEADERS, json=payload).status_code == 422
    assert path.read_bytes() == original


def test_authentication_precedes_writes(routes_api):
    client, path, _ = routes_api
    original = path.read_bytes()
    for headers in ({}, {"Authorization": "Bearer invalid"}):
        assert client.get(URL, headers=headers).status_code == 401
        assert client.put(URL, headers=headers, json={"models": {}}).status_code == 401
    assert path.read_bytes() == original


def test_path_is_service_configured_and_independent_of_env(tmp_path):
    target = tmp_path / "managed.json"
    other = tmp_path / "other.json"
    initial = json.dumps({"providers": {}, "models": {"old": {}}})
    target.write_text(initial)
    other.write_text(initial)
    env = tmp_path / ".env"
    env.write_text(f"LLM_CHAT_ROUTES_FILE={other}\n")
    settings = OpsSettings.from_env(
        {
            "OPS_API_TOKEN": TOKEN,
            "OPS_MODEL_ROUTES_FILE": "managed.json",
        },
        repo_root=tmp_path,
    )
    assert settings.model_routes_file == target
    client = TestClient(create_app(settings))
    response = client.patch(
        "/api/v1/nonebot/config",
        headers=HEADERS,
        json={"LLM_CHAT_ROUTES_FILE": str(tmp_path / "third.json")},
    )
    assert response.status_code == 200
    assert client.put(URL, headers=HEADERS, json={"models": {}}).status_code == 200
    assert json.loads(target.read_text())["models"] == {}
    assert client.get(URL, headers=HEADERS).json() == {"models": {}}
    assert other.read_text() == initial


def test_unconfigured_path_returns_503(tmp_path):
    settings = OpsSettings.from_env({"OPS_API_TOKEN": TOKEN}, repo_root=tmp_path)
    assert settings.model_routes_file is None
    client = TestClient(create_app(settings))
    assert client.get(URL, headers=HEADERS).status_code == 503
    assert client.put(URL, headers=HEADERS, json={"models": {}}).status_code == 503


@pytest.mark.parametrize(
    "contents", [None, "{broken", "[]", '{"providers": [] ,"models": {}}']
)
def test_unavailable_or_invalid_file_returns_503(routes_api, contents):
    client, path, _ = routes_api
    if contents is None:
        path.unlink()
    else:
        path.write_text(contents)
    assert client.get(URL, headers=HEADERS).status_code == 503
    assert client.put(URL, headers=HEADERS, json={"models": {}}).status_code == 503
    if contents is not None:
        assert path.read_text() == contents


def test_failed_atomic_replace_preserves_original(routes_api, monkeypatch):
    client, path, _ = routes_api
    original = path.read_bytes()

    def fail(*args):
        raise OSError("disk failure")

    monkeypatch.setattr("ops_api.model_routes_store.os.replace", fail)
    assert client.put(URL, headers=HEADERS, json={"models": {}}).status_code == 503
    assert path.read_bytes() == original
    assert not list(path.parent.glob(".ops-model-routes-*"))
