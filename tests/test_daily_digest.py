from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from multi_llm_chat.daily_digest import (
    DailyDigestConfig,
    DailyDigestLimits,
    DailyDigestService,
    DigestItem,
    DigestSelection,
    FeedSource,
    HtmlSource,
    JsonNewsSource,
    SelectedDigestItem,
    WeatherReport,
    load_daily_digest_config,
    parse_anilist_items,
    parse_feed_items,
    parse_html_items,
    parse_json_news_items,
    render_digest,
    validate_selection,
)
from multi_llm_chat.models import AssistantTurn

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 25, 10, 0, tzinfo=SHANGHAI)


def make_item(
    item_id: str,
    category: str,
    *,
    source_name: str = "测试来源",
    title: str = "测试资讯",
    url: str | None = None,
) -> DigestItem:
    return DigestItem(
        item_id=item_id,
        category=category,
        source_id="source",
        source_name=source_name,
        title=title,
        summary="摘要",
        published_at=NOW - timedelta(hours=1),
        url=url or f"https://example.com/{item_id}",
    )


def test_daily_digest_config_only_enables_explicit_group(tmp_path: Path) -> None:
    path = tmp_path / "daily.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "timezone": "Asia/Shanghai",
                "send_time": {"hour": 10, "minute": 0},
                "groups": [
                    {
                        "group_id": "748245950",
                        "city": "上海",
                        "latitude": 31.2304,
                        "longitude": 121.4737,
                        "enabled": True,
                    }
                ],
                "limits": {},
            }
        ),
        encoding="utf-8",
    )

    config = load_daily_digest_config(path)

    assert config.send_time.hour == 10
    assert [group.group_id for group in config.groups if group.enabled] == ["748245950"]
    assert config.limits.max_ai_items == 1


def test_native_feed_is_parsed() -> None:
    source = FeedSource(
        source_id="openai_news",
        source_name="OpenAI",
        category="ai",
        url="https://openai.com/news/rss.xml",
    )
    body = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><title>OpenAI</title><item>
      <title>Introducing a new model</title>
      <link>https://openai.com/index/new-model/</link>
      <description><![CDATA[<p>A useful model release.</p>]]></description>
      <pubDate>Tue, 25 Aug 2026 00:30:00 GMT</pubDate>
    </item></channel></rss>"""

    items = parse_feed_items(source, body, SHANGHAI)

    assert len(items) == 1
    assert items[0].source_id == "openai_news"
    assert items[0].title == "Introducing a new model"
    assert items[0].summary == "A useful model release."
    assert items[0].published_at.hour == 8


def test_official_page_structured_data_is_parsed() -> None:
    source = HtmlSource(
        source_id="wow_cn",
        source_name="魔兽世界国服",
        category="game",
        url="https://wow.blizzard.cn/news/",
    )
    body = """
    <html><head><script type="application/ld+json">
    {
      "@type": "NewsArticle",
      "headline": "新赛季内容更新",
      "description": "新副本与职业调整上线",
      "datePublished": "2026-08-25T08:00:00+08:00",
      "url": "/news/12345/"
    }
    </script></head><body></body></html>
    """.encode()

    items = parse_html_items(source, body, SHANGHAI)

    assert len(items) == 1
    assert items[0].title == "新赛季内容更新"
    assert items[0].url == "https://wow.blizzard.cn/news/12345/"


def test_html_date_is_not_inherited_from_unrelated_outer_container() -> None:
    source = HtmlSource(
        source_id="league_riot",
        source_name="英雄联盟官方",
        category="game",
        url="https://example.com/news/",
    )
    body = """
    <div>
      <a href="/news/old">没有日期的旧文章</a>
      <a href="/news/current">2026-08-25T08:00:00+08:00 当日版本公告</a>
    </div>
    """.encode()

    items = parse_html_items(source, body, SHANGHAI)

    assert len(items) == 1
    assert items[0].url == "https://example.com/news/current"


def test_mihoyo_public_content_api_is_parsed() -> None:
    source = JsonNewsSource(
        source_id="star_rail",
        source_name="崩坏：星穹铁道",
        category="game",
        url="https://example.com/list",
        parser="mihoyo",
        detail_url="https://sr.mihoyo.com/news/{id}",
    )
    data = {
        "retcode": 0,
        "data": {
            "list": [
                {
                    "iInfoId": 165873,
                    "sTitle": "4.5版本预下载&更新预告",
                    "sIntro": "版本预下载现已开启。",
                    "dtStartTime": "2026-08-24 14:00:00",
                }
            ]
        },
    }

    items = parse_json_news_items(source, data, SHANGHAI)

    assert len(items) == 1
    assert items[0].title == "4.5版本预下载&更新预告"
    assert items[0].url == "https://sr.mihoyo.com/news/165873"


def test_china_government_public_policy_json_is_parsed() -> None:
    source = JsonNewsSource(
        source_id="china_policy",
        source_name="中国政府网",
        category="policy",
        url="https://www.gov.cn/zhengce/zuixin/ZUIXINZHENGCE.json",
        parser="china_policy",
        detail_url="{url}",
    )
    data = [
        {
            "TITLE": "国务院关于修改住房公积金管理条例的决定",
            "SUB_TITLE": "",
            "URL": "https://www.gov.cn/zhengce/example.htm",
            "DOCRELPUBTIME": "2026-08-24",
        }
    ]

    items = parse_json_news_items(source, data, SHANGHAI)

    assert len(items) == 1
    assert items[0].source_name == "中国政府网"
    assert items[0].url == "https://www.gov.cn/zhengce/example.htm"


def test_anilist_schedule_returns_ranked_daily_airings() -> None:
    data = {
        "data": {
            "Page": {
                "airingSchedules": [
                    {
                        "airingAt": int(NOW.timestamp()),
                        "episode": 8,
                        "media": {
                            "id": 123,
                            "title": {
                                "english": "Test Anime",
                                "romaji": "Test Anime",
                                "native": "テストアニメ",
                            },
                            "siteUrl": "https://anilist.co/anime/123",
                            "popularity": 20000,
                            "averageScore": 82,
                        },
                    }
                ]
            }
        }
    }

    items = parse_anilist_items(data, NOW)

    assert [item.title for item in items] == ["Test Anime"]
    assert "第 8 话今日放送" in items[0].summary
    assert items[0].url == "https://anilist.co/anime/123"


def test_selection_enforces_one_ai_item_limit() -> None:
    candidates = [
        make_item("ai-1", "ai"),
        make_item("ai-2", "ai"),
    ]
    selection = DigestSelection(
        items=[
            SelectedDigestItem(item_id="ai-1", text="第一条 AI 更新"),
            SelectedDigestItem(item_id="ai-2", text="第二条 AI 更新"),
        ]
    )

    assert validate_selection(selection, candidates, DailyDigestLimits()) == [
        selection.items[0]
    ]


@pytest.mark.asyncio
async def test_llm_selects_from_candidates_without_builtin_tools() -> None:
    calls = []

    class Provider:
        async def complete(self, **kwargs):
            calls.append(kwargs)
            return AssistantTurn(
                content=json.dumps(
                    {
                        "items": [
                            {
                                "item_id": "ai-1",
                                "text": "一个重要模型正式发布，开发者可关注其能力变化。",
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            )

    service = DailyDigestService(
        provider=Provider(),
        config=DailyDigestConfig(groups=[]),
        logger=SimpleNamespace(),
    )

    selected = await service._select_items([make_item("ai-1", "ai")], NOW)

    assert selected[0].item_id == "ai-1"
    assert calls[0]["request_type"] == "daily_digest"
    assert calls[0]["allow_builtin_tools"] is False
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert "tools" not in calls[0]


def test_render_includes_direct_source_and_partial_failures() -> None:
    candidate = make_item(
        "game-1",
        "game",
        source_name="魔兽世界国服",
        url="https://wow.blizzard.cn/news/123/",
    )
    weather = WeatherReport(
        source_id="open_meteo",
        source_name="Open-Meteo",
        city="上海",
        condition="有雨",
        temperature_min=25,
        temperature_max=32,
        apparent_temperature_min=27,
        apparent_temperature_max=36,
        precipitation_probability=70,
        wind_speed_max=18,
    )

    message = render_digest(
        current=NOW,
        city="上海",
        weather=weather,
        candidates=[candidate],
        selected=[SelectedDigestItem(item_id="game-1", text="新赛季今天上线。")],
        failed_source_names=["Anthropic", "英雄联盟国服"],
        limits=DailyDigestLimits(),
    )

    assert "上海早报 · 08月25日" in message
    assert "通勤记得带伞" in message
    assert "魔兽世界国服：https://wow.blizzard.cn/news/123/" in message
    assert "来源异常：Anthropic、英雄联盟国服" in message


def test_render_drops_lowest_ranked_items_to_respect_hard_limit() -> None:
    candidates = [
        make_item(
            f"game-{index}",
            "game",
            url=f"https://example.com/{'x' * 280}/{index}",
        )
        for index in range(3)
    ]
    selected = [
        SelectedDigestItem(item_id=item.item_id, text="重要更新" * 20)
        for item in candidates
    ]
    limits = DailyDigestLimits(target_chars=600, hard_max_chars=600)

    message = render_digest(
        current=NOW,
        city="上海",
        weather=None,
        candidates=candidates,
        selected=selected,
        failed_source_names=[],
        limits=limits,
    )

    assert len(message) <= 600
    assert message.endswith("/0")
    assert not message.endswith("/2")
