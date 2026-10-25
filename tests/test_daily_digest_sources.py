from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest
from multi_llm_chat.daily_digest import (
    ESPORTS_SOURCES,
    HTML_SOURCES,
    JSON_NEWS_SOURCES,
    DailyDigestConfig,
    DailyDigestService,
    _deduplicate_items,
    _is_candidate,
    parse_esports_items,
    parse_json_news_items,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 25, 10, tzinfo=SHANGHAI)
LEAGUE_NEWS = next(
    source for source in JSON_NEWS_SOURCES if source.source_id == "league_cn"
)
VALORANT_NEWS = next(
    source for source in JSON_NEWS_SOURCES if source.source_id == "valorant_cn"
)
LEAGUE, VALORANT = ESPORTS_SOURCES


def league_match(**updates):
    return {
        "isTft": "0",
        "bMatchId": "13636",
        "bMatchName": "TLAW vs BLG",
        "TeamShortNameA": "",
        "TeamShortNameB": "BLG",
        "GameName": "2026全球总决赛",
        "GameModeName": "BO1",
        "GameTypeName": "瑞士轮",
        "GameProcName": "第二天",
        "ScoreA": "0",
        "ScoreB": "1",
        "MatchStatus": "3",
        "MatchDate": "2026-10-25 08:00:00",
        **updates,
    }


def valorant_match(**updates):
    return {
        "bMatchId": 1002764,
        "secondLevelGameId": 1000075,
        "secondLevelGameName": "2026CN联赛晋升赛",
        "bMatchName": "败者组决赛",
        "matchStatusId": 3,
        "scoreA": 0,
        "scoreB": 2,
        "matchFormat": "BO3",
        "matchDate": "2026-10-24T17:00:00+08:00",
        "teamAId": 40,
        "teamBId": 30,
        "teamA": {"teamSpName": "KBG", "teamName": "KEEPBEST GAMING"},
        "teamB": {"teamSpName": "NOVA", "teamName": "NOVA ESPORTS"},
        **updates,
    }


def test_league_cmc_news_uses_original_date_redirect_and_video_fallback():
    record = {
        "iDocID": "9107763462516557080",
        "sTitle": "26.21版本更新公告",
        "sIdxTime": "2026-10-21 20:47:28",
        "sDesc": "<p>版本调整</p>",
        "sRedirectURL": "https://lol.qq.com/gicp/news/410/37099805.html",
    }
    data = {
        "status": 1,
        "data": {
            "result": [
                record,
                {
                    **record,
                    "iDocID": "2",
                    "sTitle": "官方赛事视频",
                    "sRedirectURL": "",
                    "sVID": "video-id",
                },
                {**record, "iDocID": "3", "sRedirectURL": "javascript:alert(1)"},
            ]
        },
    }
    items = parse_json_news_items(LEAGUE_NEWS, data, SHANGHAI)
    assert len(items) == 3
    assert items[0].url == record["sRedirectURL"]
    assert items[0].summary == "版本调整"
    assert items[0].published_at == datetime(2026, 10, 21, 20, 47, 28, tzinfo=SHANGHAI)
    assert not _is_candidate(items[0], NOW - timedelta(days=1), NOW)
    assert items[1].url == "https://lol.qq.com/v/v2/detail.shtml?docid=2"
    assert items[2].url == "https://lol.qq.com/news/detail.shtml?docid=3"
    assert all(source.source_id != "league_riot" for source in HTML_SOURCES)


def test_existing_valorant_news_items_envelope_still_works():
    items = parse_json_news_items(
        VALORANT_NEWS,
        {
            "status": "0",
            "data": {
                "items": [
                    {
                        "iDocID": "123",
                        "sTitle": "新版本维护公告",
                        "sCreated": "2026-10-24 18:00:00",
                    }
                ]
            },
        },
        SHANGHAI,
    )
    assert len(items) == 1
    assert items[0].url == "https://val.qq.com/newsdetails.html?docid=123&goback=main"


@pytest.mark.parametrize(
    "data",
    [
        {"status": 500, "data": {"result": []}},
        {"status": 0, "data": {"result": []}},
        {"status": 1, "data": {}},
        {"status": 1, "data": None},
    ],
)
def test_cmc_errors_are_not_treated_as_empty_success(data):
    with pytest.raises((TypeError, ValueError)):
        parse_json_news_items(LEAGUE_NEWS, data, SHANGHAI)


def test_league_results_scan_unsorted_history_and_keep_distinct_match_links():
    old = [
        league_match(bMatchId=str(index), MatchDate="2024-10-26 20:00:00")
        for index in range(40)
    ]
    data = {
        "status": "0",
        "lastUpTime": NOW.isoformat(),
        "msg": [
            *old,
            league_match(),
            league_match(
                bMatchId="13635",
                bMatchName="DK vs T1",
                TeamShortNameB="",
                ScoreA="1",
                ScoreB="0",
            ),
        ],
    }
    items = parse_esports_items(LEAGUE, data, SHANGHAI)
    recent = [
        item for item in items if _is_candidate(item, NOW - timedelta(days=1), NOW)
    ]
    assert len(recent) == 2
    assert recent[0].category == "game"
    assert recent[0].title == "英雄联盟｜2026全球总决赛：TLAW 0:1 BLG"
    assert "瑞士轮 / 第二天" in recent[0].summary
    assert recent[0].published_at == datetime(2026, 10, 25, 8, tzinfo=SHANGHAI)
    assert recent[0].url == "https://lpl.qq.com/web202301/stats.shtml?bmid=13636"
    assert len(_deduplicate_items(recent)) == 2


@pytest.mark.parametrize(
    "updates",
    [
        {"MatchStatus": "1"},
        {"MatchStatus": 2},
        {"MatchStatus": "4"},
        {"isTft": "1"},
        {"ScoreB": None},
        {"ScoreB": -1},
        {"ScoreA": "0", "ScoreB": "0"},
        {"MatchDate": ""},
        {"bMatchId": ""},
        {"bMatchName": "待定", "TeamShortNameB": ""},
    ],
)
def test_league_does_not_publish_live_or_incomplete_scores(updates):
    assert (
        parse_esports_items(
            LEAGUE, {"status": 0, "msg": [league_match(**updates)]}, SHANGHAI
        )
        == []
    )


def test_valorant_results_preserve_timezone_and_series_score():
    items = parse_esports_items(
        VALORANT, {"status": 0, "msg": [valorant_match()]}, SHANGHAI
    )
    assert len(items) == 1
    assert items[0].title == "无畏契约｜2026CN联赛晋升赛：KBG 0:2 NOVA"
    assert items[0].published_at == datetime(2026, 10, 24, 17, tzinfo=SHANGHAI)
    assert "系列赛总比分" in items[0].summary
    assert (
        items[0].url
        == "https://vct.qq.com/match-data.html?gameId=1000075&matchId=1002764&teamAId=40&teamBId=30"
    )
    assert _is_candidate(items[0], NOW - timedelta(days=1), NOW)


@pytest.mark.parametrize(
    "updates",
    [
        {"matchStatusId": 1},
        {"matchStatusId": "2"},
        {"scoreA": None},
        {"scoreB": True},
        {"teamA": None},
        {"teamB": {}},
        {"teamB": {"teamSpName": "TBD"}},
        {"teamAId": None},
        {"secondLevelGameId": None},
    ],
)
def test_valorant_skips_unfinished_or_incomplete_matches(updates):
    assert (
        parse_esports_items(
            VALORANT, {"status": 0, "msg": [valorant_match(**updates)]}, SHANGHAI
        )
        == []
    )


@pytest.mark.parametrize(
    "source,record,date_key",
    [
        (LEAGUE, league_match(), "MatchDate"),
        (VALORANT, valorant_match(), "matchDate"),
    ],
)
def test_match_feed_update_time_does_not_make_old_or_future_results_current(
    source, record, date_key
):
    for date in ["2024-10-21T20:00:00+08:00", "2026-10-26T20:00:00+08:00"]:
        data = {
            "status": 0,
            "lastUpdateTime": NOW.isoformat(),
            "msg": [{**record, date_key: date}],
        }
        (item,) = parse_esports_items(source, data, SHANGHAI)
        assert not _is_candidate(item, NOW - timedelta(days=1), NOW)


@pytest.mark.asyncio
async def test_esports_sources_participate_in_collection_and_fail_independently(
    monkeypatch,
):
    service = DailyDigestService(None, DailyDigestConfig(groups=[]), Mock())
    monkeypatch.setattr("multi_llm_chat.daily_digest.FEED_SOURCES", ())
    monkeypatch.setattr("multi_llm_chat.daily_digest.HTML_SOURCES", ())
    monkeypatch.setattr("multi_llm_chat.daily_digest.JSON_NEWS_SOURCES", ())
    service._collect_weather_safely = AsyncMock(return_value=None)
    service._collect_anilist_safely = AsyncMock(return_value=Mock(items=[]))

    async def fetch(session, url):
        if url == VALORANT.url:
            return b'{"status": 500, "msg": []}'
        data = {
            "status": 0,
            "msg": [
                league_match(),
                league_match(bMatchId="1", MatchDate="2024-10-21 12:00:00"),
                league_match(bMatchId="2", MatchStatus="2"),
            ],
        }
        return json.dumps(data).encode()

    monkeypatch.setattr("multi_llm_chat.daily_digest._fetch_bytes", fetch)
    collection = await service.collect(None, NOW)
    assert len(collection.items) == 1
    assert collection.items[0].source_id == LEAGUE.source_id
    service._logger.warning.assert_called_once()
