import importlib
import json
from pathlib import Path

import nonebot
import peteralbus_wife.config as config_module
import pytest
from peteralbus_wife.config import Config, resolve_plugin_path


def test_relative_paths_are_independent_of_working_directory(tmp_path, monkeypatch):
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    monkeypatch.setattr(config_module, "PLUGIN_DIR", plugin_dir)
    monkeypatch.chdir(other_dir)

    assert resolve_plugin_path("resources", "PETERALBUS_WIFE_RES") == (
        plugin_dir / "resources"
    )
    assert resolve_plugin_path(
        str(tmp_path / "jobs"), "PETERALBUS_WIFE_JM_WORK_DIR"
    ) == (tmp_path / "jobs")


@pytest.mark.parametrize("value", ["", "  "])
def test_unconfigured_resource_path_reports_the_setting(value):
    with pytest.raises(ValueError, match="PETERALBUS_WIFE_RES"):
        resolve_plugin_path(value, "PETERALBUS_WIFE_RES")


def test_resource_path_expands_home():
    assert (
        resolve_plugin_path("~/pictures", "PETERALBUS_WIFE_RES")
        == (Path.home() / "pictures").resolve()
    )


@pytest.mark.parametrize(
    "character", ["亚妮艾丝", "亚尔缇娜", "鉴纯夏", "雪之下雪乃", "流萤"]
)
def test_both_picture_commands_use_configured_resource_root(
    tmp_path, monkeypatch, character
):
    nonebot.init(_env_file=None)
    handler = importlib.import_module("peteralbus_wife.handler")
    image = tmp_path / character / "picture.png"
    image.parent.mkdir()
    image.write_bytes(b"picture")
    monkeypatch.setattr(handler, "config", Config(peteralbus_wife_res=str(tmp_path)))
    monkeypatch.setattr(
        handler.random,
        "choice",
        lambda items: character if isinstance(items[0], str) else items[0],
    )

    assert handler.random_wife_pic() == (character, image)
    assert handler.get_agnes_pic() == (character, image)


def test_jm_jobs_use_configured_root_and_distinct_directories(tmp_path, monkeypatch):
    nonebot.init(_env_file=None)
    downloader = importlib.import_module("peteralbus_wife.jm_downloader")
    option_path = tmp_path / "download.json"
    option_path.write_text(
        json.dumps({"dir_rule": {"rule": "Bd_Atitle_Pindex"}}), encoding="utf-8"
    )
    work_root = tmp_path / "JM 任务"
    monkeypatch.setattr(
        downloader,
        "config",
        Config(
            peteralbus_wife_jm_option_path=str(option_path),
            peteralbus_wife_jm_work_dir=str(work_root),
        ),
    )
    option, root, first = downloader._prepare_job("123")
    _, second_root, second = downloader._prepare_job("123")

    assert root == second_root == work_root
    assert first.parent == second.parent == work_root
    assert first != second
    assert first.is_dir() and second.is_dir()
    assert option.dir_rule.base_dir == str(first)
    downloader._cleanup_job_dir(first, root)
    assert not first.exists()
    assert second.is_dir()


def test_jm_requires_work_directory_before_creating_a_job(tmp_path, monkeypatch):
    nonebot.init(_env_file=None)
    downloader = importlib.import_module("peteralbus_wife.jm_downloader")
    monkeypatch.setattr(downloader, "config", Config())
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="PETERALBUS_WIFE_JM_WORK_DIR"):
        downloader._prepare_job("123")

    assert list(tmp_path.iterdir()) == []
