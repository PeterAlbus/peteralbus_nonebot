from __future__ import annotations

import signal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from ops_api.app import create_app
from ops_api.config_store import EnvConfigStore
from ops_api.settings import OpsSettings
from ops_api.supervisor import OpsActionError, OpsSupervisor

TOKEN = "a" * 64


class FakeSupervisor:
    def __init__(self) -> None:
        self.restarts: list[str] = []

    def get_system_status(self) -> dict[str, Any]:
        return {"platform": "test"}

    def get_nonebot_status(self) -> dict[str, Any]:
        return {"service": {"state": "running"}}

    def get_napcat_status(self) -> dict[str, Any]:
        return {"process": {"running": True}}

    def restart_nonebot(self) -> dict[str, Any]:
        self.restarts.append("nonebot")
        return {"restarted": True}

    def restart_napcat(self) -> dict[str, Any]:
        self.restarts.append("napcat")
        return {"restarted": True}


def _settings(root: Path, env_file: Path) -> OpsSettings:
    return OpsSettings(
        repo_root=root,
        api_token=TOKEN,
        nonebot_env_file=env_file,
        nonebot_log_file=root / "nonebot.log",
        napcat_stdout_log=root / "napcat.log",
        napcat_account="123456789",
    )


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def test_all_operations_require_bearer_token(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("HOST=127.0.0.1\n", encoding="utf-8")
    supervisor = FakeSupervisor()
    client = TestClient(create_app(_settings(tmp_path, env_file), supervisor))

    response = client.post("/api/v1/nonebot/restart")

    assert response.status_code == 401
    assert supervisor.restarts == []


def test_status_and_restart_routes_are_authenticated(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("HOST=127.0.0.1\n", encoding="utf-8")
    supervisor = FakeSupervisor()
    client = TestClient(create_app(_settings(tmp_path, env_file), supervisor))

    assert client.get("/api/v1/system", headers=_headers()).json() == {
        "platform": "test"
    }
    assert client.get("/api/v1/nonebot", headers=_headers()).status_code == 200
    assert client.get("/api/v1/napcat", headers=_headers()).status_code == 200
    assert client.post("/api/v1/napcat/restart", headers=_headers()).json() == {
        "restarted": True
    }
    assert client.post("/api/v1/nonebot/restart", headers=_headers()).json() == {
        "restarted": True
    }
    assert supervisor.restarts == ["napcat", "nonebot"]


def test_config_read_redacts_credentials_and_patch_updates_them_write_only(
    tmp_path: Path,
) -> None:
    template = tmp_path / ".env.example"
    template.write_text(
        'HOST="127.0.0.1"\nPORT=2333\nLLM_CHAT_MODEL="model-a"\n'
        "LLM_CHAT_ARK_API_KEY=\nLLM_CHAT_WHITELIST=[]\n"
        "# LLM_CHAT_IMAGE_UNDERSTANDING=true\n",
        encoding="utf-8",
    )
    env_file = tmp_path / ".env"
    env_file.write_text(
        'HOST="127.0.0.1"\nPORT=2333\nLLM_CHAT_MODEL="model-a"\n'
        "LLM_CHAT_ARK_API_KEY='old-secret-value'\nLLM_CHAT_WHITELIST=[]\n",
        encoding="utf-8",
    )
    settings = _settings(tmp_path, env_file)
    client = TestClient(create_app(settings, FakeSupervisor()))

    read = client.get("/api/v1/nonebot/config", headers=_headers()).json()
    assert read["config"]["LLM_CHAT_ARK_API_KEY"] == {
        "configured": True,
        "redacted": True,
    }

    response = client.patch(
        "/api/v1/nonebot/config",
        headers=_headers(),
        json={
            "LLM_CHAT_MODEL": "model-b",
            "LLM_CHAT_ARK_API_KEY": "new-secret-value",
            "LLM_CHAT_WHITELIST": ["123", "456"],
            "LLM_CHAT_IMAGE_UNDERSTANDING": False,
        },
    )

    assert response.status_code == 200
    assert response.json()["restart_required"] is True
    reread = client.get("/api/v1/nonebot/config", headers=_headers()).json()
    assert reread["config"]["LLM_CHAT_MODEL"] == "model-b"
    assert reread["config"]["LLM_CHAT_WHITELIST"] == ["123", "456"]
    assert reread["config"]["LLM_CHAT_IMAGE_UNDERSTANDING"] is False
    assert reread["config"]["LLM_CHAT_ARK_API_KEY"]["configured"] is True
    assert "new-secret-value" not in response.text
    assert "new-secret-value" not in reread.__repr__()
    assert env_file.stat().st_mode & 0o777 == 0o600


def test_config_rejects_unknown_fields_newlines_and_non_loopback_host(
    tmp_path: Path,
) -> None:
    template = tmp_path / ".env.example"
    template.write_text('HOST="127.0.0.1"\nLLM_CHAT_MODEL="model-a"\n', encoding="utf-8")
    env_file = tmp_path / ".env"
    original = 'HOST="127.0.0.1"\nLLM_CHAT_MODEL="model-a"\n'
    env_file.write_text(original, encoding="utf-8")
    store = EnvConfigStore(env_file, template)

    for patch in (
        {"ARBITRARY_ENV": "value"},
        {"LLM_CHAT_MODEL": "model-b\nPORT=99"},
        {"HOST": "0.0.0.0"},
    ):
        try:
            store.update(patch)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid config patch was accepted")
        assert env_file.read_text(encoding="utf-8") == original


def test_settings_require_strong_token_and_loopback_host(tmp_path: Path) -> None:
    try:
        OpsSettings(repo_root=tmp_path, api_token="short")
    except ValueError:
        pass
    else:
        raise AssertionError("short bearer token was accepted")

    try:
        OpsSettings(repo_root=tmp_path, api_token=TOKEN, api_host="0.0.0.0")
    except ValueError:
        pass
    else:
        raise AssertionError("non-loopback bind address was accepted")


def test_napcat_restart_targets_configured_account_and_uses_quick_login(
    tmp_path: Path, monkeypatch: Any
) -> None:
    binary = tmp_path / "QQ"
    binary.write_text("test executable", encoding="utf-8")
    app_bundle = tmp_path / "QQ.app"
    app_bundle.mkdir()
    settings = _settings(tmp_path, tmp_path / ".env")
    settings = OpsSettings(
        repo_root=settings.repo_root,
        api_token=TOKEN,
        nonebot_env_file=settings.nonebot_env_file,
        napcat_account="123456789",
        napcat_qq_app=app_bundle,
        napcat_qq_binary=binary,
        napcat_stdout_log=tmp_path / "napcat.log",
        shutdown_timeout_seconds=1,
        startup_timeout_seconds=1,
    )
    supervisor = OpsSupervisor(settings)
    process_checks = iter([([321], []), ([], [])])
    monkeypatch.setattr(supervisor, "_qq_processes", lambda: next(process_checks))
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr("ops_api.supervisor.os.kill", lambda pid, sig: killed.append((pid, sig)))

    launch_calls: list[list[str]] = []

    def fake_run(args: list[str], timeout: float) -> Any:
        launch_calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("ops_api.supervisor._run", fake_run)
    monkeypatch.setattr(
        supervisor,
        "get_napcat_status",
        lambda: {
            "process": {"running": True, "pids": [654]},
            "webui": {"listening": True},
            "http_api": {"listening": True},
            "login": {"state": "online"},
        },
    )

    result = supervisor.restart_napcat()

    assert killed == [(321, signal.SIGTERM)]
    assert launch_calls == [
        [
            "open",
            "-a",
            str(app_bundle),
            "--args",
            "--no-sandbox",
            "-q",
            "123456789",
        ]
    ]
    assert result["restarted"] is True
    assert result["ready"] is True


def test_napcat_restart_refuses_to_stop_another_qq_account(
    tmp_path: Path, monkeypatch: Any
) -> None:
    binary = tmp_path / "QQ"
    binary.write_text("test executable", encoding="utf-8")
    app_bundle = tmp_path / "QQ.app"
    app_bundle.mkdir()
    settings = _settings(tmp_path, tmp_path / ".env")
    settings = OpsSettings(
        repo_root=settings.repo_root,
        api_token=TOKEN,
        napcat_account="123456789",
        napcat_qq_app=app_bundle,
        napcat_qq_binary=binary,
    )
    supervisor = OpsSupervisor(settings)
    monkeypatch.setattr(supervisor, "_qq_processes", lambda: ([], ["pid=987"]))

    try:
        supervisor.restart_napcat()
    except OpsActionError as exc:
        assert exc.status_code == 409
    else:
        raise AssertionError("restart was allowed to target an unknown QQ account")
