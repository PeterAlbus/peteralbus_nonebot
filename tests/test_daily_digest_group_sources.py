import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest
from multi_llm_chat import daily_digest as digest
from multi_llm_chat.models import AssistantTurn
from pydantic import ValidationError

NOW = datetime(2026, 11, 20, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
DISABLED = {"league_cn", "league_esports_cn", "valorant_esports_cn", "endfield"}


def test_old_group_configuration_keeps_all_sources():
    group = digest.DailyDigestGroup(group_id="123")
    assert group.enabled_source_ids() == set(digest.SOURCE_NAMES)


def test_include_and_exclude_each_source_independently():
    ids = ["valorant_cn", "valorant_esports_cn", "open_meteo"]
    group = digest.DailyDigestGroup(
        group_id="123", source_ids=ids, disabled_source_ids=["valorant_esports_cn"]
    )
    assert group.enabled_source_ids() == {"valorant_cn", "open_meteo"}
    group = digest.DailyDigestGroup(
        group_id="123", source_ids=[" google_ai ", "google_ai", "openai_news"]
    )
    assert group.source_ids == ["google_ai", "openai_news"]


@pytest.mark.parametrize("field", ["source_ids", "disabled_source_ids"])
@pytest.mark.parametrize("value", [["unknown_source"], [""]])
def test_unknown_source_id_is_rejected(field, value):
    with pytest.raises(ValidationError, match="未知日报数据源"):
        digest.DailyDigestGroup(group_id="123", **{field: value})


def test_duplicate_group_ids_are_rejected():
    with pytest.raises(ValidationError, match="重复 group_id"):
        digest.DailyDigestConfig(groups=[{"group_id": "123"}, {"group_id": "123"}])


def test_repository_configuration_enables_new_group_with_only_four_sources_off():
    path = (
        Path(__file__).resolve().parents[1]
        / "my-bot/plugins/multi_llm_chat/daily_digest_config.json"
    )
    config = digest.load_daily_digest_config(path)
    groups = {group.group_id: group for group in config.groups}
    assert groups["748245950"].enabled_source_ids() == set(digest.SOURCE_NAMES)
    group = groups["1103699359"]
    assert group.enabled
    assert group.enabled_source_ids() == set(digest.SOURCE_NAMES) - DISABLED
    assert {
        "valorant_cn",
        "valorant_riot",
        "open_meteo",
        "anilist_schedule",
    } <= group.enabled_source_ids()


@pytest.fixture
def collection(monkeypatch):
    called = []
    provider = Mock(
        complete=AsyncMock(return_value=AssistantTurn(content='{"items": []}'))
    )
    service = digest.DailyDigestService(provider, digest.DailyDigestConfig(), Mock())

    def item(source_id, category="game"):
        return digest.DigestItem(
            item_id=source_id,
            source_id=source_id,
            source_name=digest.SOURCE_NAMES[source_id],
            category=category,
            title=f"{source_id} 发布模型版本 社保调整",
            published_at=NOW - timedelta(hours=1),
            url=f"https://example.com/{source_id}",
        )

    async def source(session, source):
        called.append(source.source_id)
        return digest.SourceResult(
            source.source_id,
            source.source_name,
            [item(source.source_id, getattr(source, "category", "game"))],
        )

    async def weather(session, group):
        called.append("open_meteo")
        return digest.WeatherReport(
            source_id="open_meteo",
            source_name="Open-Meteo",
            city=group.city,
            condition="晴",
            temperature_min=10,
            temperature_max=20,
            apparent_temperature_min=10,
            apparent_temperature_max=20,
            precipitation_probability=0,
            wind_speed_max=10,
        )

    async def anime(session, now):
        called.append("anilist_schedule")
        return digest.SourceResult(
            "anilist_schedule", "AniList", [item("anilist_schedule", "anime")]
        )

    for method in [
        "_collect_feed_safely",
        "_collect_html_safely",
        "_collect_json_news_safely",
        "_collect_esports_safely",
    ]:
        monkeypatch.setattr(service, method, source)
    monkeypatch.setattr(service, "_collect_weather_safely", weather)
    monkeypatch.setattr(service, "_collect_anilist_safely", anime)
    return service, provider, called


@pytest.mark.asyncio
async def test_disabling_sources_skips_requests_and_model_candidates_per_group(
    collection,
):
    service, provider, called = collection
    restricted = digest.DailyDigestGroup(
        group_id="1103699359", disabled_source_ids=sorted(DISABLED)
    )
    await service.build_message(restricted, NOW)
    assert set(called) == set(digest.SOURCE_NAMES) - DISABLED
    prompt = json.loads(provider.complete.call_args.kwargs["messages"][1]["content"])
    assert {item["item_id"] for item in prompt["candidates"]} == set(
        digest.SOURCE_NAMES
    ) - DISABLED - {"open_meteo"}

    called.clear()
    original = digest.DailyDigestGroup(group_id="748245950")
    results = await service.collect(original, NOW)
    assert set(called) == set(digest.SOURCE_NAMES)
    assert DISABLED <= {item.source_id for item in results.items}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_ids",
    [
        [],
        ["open_meteo"],
        ["anilist_schedule"],
        ["google_ai"],
        ["star_rail", "zenless_zone_zero"],
    ],
)
async def test_whitelist_can_select_weather_anime_and_individual_news_sources(
    collection, source_ids
):
    service, _, called = collection
    group = digest.DailyDigestGroup(group_id="123", source_ids=source_ids)
    results = await service.collect(group, NOW)
    assert set(called) == set(source_ids)
    assert (results.weather is not None) == ("open_meteo" in source_ids)
    assert {item.source_id for item in results.items} == set(source_ids) - {
        "open_meteo"
    }


@pytest.mark.asyncio
async def test_candidates_from_disabled_sources_are_removed_even_if_a_collector_returns_them(
    collection, monkeypatch
):
    service, _, _ = collection
    group = digest.DailyDigestGroup(group_id="123", source_ids=["valorant_cn"])
    leaked_item = digest.DigestItem(
        item_id="league_cn",
        source_id="league_cn",
        source_name="英雄联盟国服官方",
        category="game",
        title="版本更新",
        published_at=NOW - timedelta(hours=1),
        url="https://example.com/league",
    )
    monkeypatch.setattr(
        service,
        "_collect_json_news_safely",
        AsyncMock(
            return_value=digest.SourceResult("valorant_cn", "VALORANT", [leaked_item])
        ),
    )
    results = await service.collect(group, NOW)
    assert results.items == []
