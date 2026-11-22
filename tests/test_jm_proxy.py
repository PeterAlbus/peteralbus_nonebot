import importlib
import json
import urllib.request

import nonebot
import pytest
from peteralbus_wife.config import Config


@pytest.fixture
def jm_job(tmp_path, monkeypatch):
    nonebot.init(_env_file=None)
    downloader = importlib.import_module("peteralbus_wife.jm_downloader")
    option_path = tmp_path / "download.json"
    option_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        downloader,
        "config",
        Config(
            peteralbus_wife_jm_option_path=str(option_path),
            peteralbus_wife_jm_work_dir=str(tmp_path / "jobs"),
        ),
    )
    # 模拟长时间运行的进程在启动时记住了已经失效的代理端口。
    monkeypatch.setattr(
        downloader.JmModuleConfig,
        "DEFAULT_PROXIES",
        {"http": "127.0.0.1:1111", "https": "127.0.0.1:1111"},
    )
    return downloader, option_path


def test_each_job_refreshes_proxy_after_port_change_and_disable(jm_job, monkeypatch):
    downloader, _ = jm_job
    settings = iter(
        [
            {"http": "http://127.0.0.1:7892"},
            {"https": "127.0.0.1:7893"},
            {},
        ]
    )
    monkeypatch.setattr(urllib.request, "getproxies", lambda: next(settings))

    first, _, _ = downloader._prepare_job("123")
    second, _, _ = downloader._prepare_job("124")
    disabled, _, _ = downloader._prepare_job("125")

    assert first.client.postman.meta_data.src_dict["proxies"] == {
        "http": "127.0.0.1:7892",
        "https": "127.0.0.1:7892",
    }
    assert second.client.postman.meta_data.src_dict["proxies"] == {
        "http": "127.0.0.1:7893",
        "https": "127.0.0.1:7893",
    }
    assert disabled.client.postman.meta_data.src_dict["proxies"] == {}


@pytest.mark.parametrize(
    "explicit_proxy",
    [
        "127.0.0.1:8888",
        {"http": "127.0.0.1:8888", "https": "127.0.0.1:8889"},
        None,
    ],
)
def test_explicit_proxy_configuration_takes_precedence(
    jm_job, monkeypatch, explicit_proxy
):
    downloader, option_path = jm_job
    option_path.write_text(
        json.dumps({"client": {"postman": {"meta_data": {"proxies": explicit_proxy}}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        urllib.request, "getproxies", lambda: {"http": "127.0.0.1:7892"}
    )

    option, _, _ = downloader._prepare_job("123")

    assert option.client.postman.meta_data.src_dict["proxies"] == explicit_proxy
