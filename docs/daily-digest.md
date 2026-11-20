# 按群配置日报数据源

NoneBot 从 `.env` 中 `LLM_CHAT_DAILY_DIGEST_CONFIG_FILE` 指定的文件读取配置。未设置时使用 `my-bot/plugins/multi_llm_chat/daily_digest_config.json`。部署时如果使用仓库外的配置，应修改那个实际运行文件。配置在启动时加载，修改后重启 NoneBot。

## 选择规则

每个群独立配置两个可选字段：

- `source_ids`：来源白名单；省略或设为 `null` 时使用全部来源，指定数组时只使用这些来源。
- `disabled_source_ids`：排除清单，默认空数组；在白名单或全部来源中进一步排除，优先级更高。

选择粒度是单个来源。例如 `valorant_cn`（国服新闻）、`valorant_riot`（繁中新闻）、`valorant_esports_cn`（官方赛果）可以分别开关。天气 `open_meteo` 和动画 `anilist_schedule` 也分别选择。来源 ID 会去除首尾空白并去重，未知 ID 会在加载时明确报错。

`source_ids: []` 表示不使用任何来源；需要停发日报时使用群的 `enabled: false`。同一个 `group_id` 只能配置一次。

未启用的来源不会发出采集请求，采集结果还会再次按群的来源清单过滤后才进入模型。定时日报、手动预览和补发共用同一份群配置。发送时间、条数和字符限制仍由顶层配置统一控制；天气使用该群的城市及经纬度。

## 当前两群配置

```json
{
  "version": 1,
  "timezone": "Asia/Shanghai",
  "send_time": {"hour": 10, "minute": 0},
  "groups": [
    {
      "group_id": "748245950",
      "city": "上海",
      "enabled": true
    },
    {
      "group_id": "1103699359",
      "city": "上海",
      "enabled": true,
      "disabled_source_ids": [
        "league_cn",
        "league_esports_cn",
        "valorant_esports_cn",
        "endfield"
      ]
    }
  ]
}
```

第一个群保留全部来源。第二个群仅排除上述四个来源，无畏契约新闻、星铁、绝区零、魔兽世界等继续采集。两个群均使用默认上海经纬度与默认条数限制。

如只需要 AI 和天气，可在某个群内设置：

```json
{
  "group_id": "1103699359",
  "source_ids": ["openai_news", "google_ai", "anthropic_news", "open_meteo"]
}
```

## 完整来源 ID

| 分类 | source_id | 来源 |
| --- | --- | --- |
| 天气 | `open_meteo` | Open-Meteo，按群经纬度查询 |
| 动画 | `anilist_schedule` | AniList 当日放送 |
| 游戏新闻 | `league_cn` | 英雄联盟国服官方 |
| 游戏新闻 | `valorant_cn` | 无畏契约国服 |
| 游戏新闻 | `valorant_riot` | VALORANT 繁中官方 |
| 游戏新闻 | `star_rail` | 崩坏：星穹铁道 |
| 游戏新闻 | `zenless_zone_zero` | 绝区零 |
| 游戏新闻 | `wow_cn` | 魔兽世界国服 |
| 游戏新闻 | `endfield` | 明日方舟：终末地 |
| 游戏赛果 | `league_esports_cn` | 英雄联盟赛事官网 |
| 游戏赛果 | `valorant_esports_cn` | 无畏契约赛事官网 |
| 单机游戏 | `pc_gamer` | PC Gamer |
| 单机游戏 | `gematsu` | Gematsu |
| AI | `openai_news` | OpenAI |
| AI | `google_ai` | Google AI |
| AI | `google_deepmind` | Google DeepMind |
| AI | `hugging_face` | Hugging Face |
| AI | `anthropic_news` | Anthropic |
| 新闻 | `xinhua_news` | 新华网 |
| 政策 | `china_policy` | 中国政府网 |
| 政策 | `shanghai_policy` | 上海市统一政策发布平台 |

采集和解析实现位于 `daily_digest.py`，来源清单由同一模块中的 `SOURCE_NAMES` 汇总。选择某来源不保证该来源每天都有符合时间窗口和编辑规则的内容进入最终日报。
