# peteralbus_nonebot

这是一个基于 NoneBot 2 和 OneBot V11 的私人 QQ 机器人项目。`multi_llm_chat` 插件负责群聊上下文、模型路由、工具调用、动态群记忆和回复去重。

## 安装与启动

项目要求 Python 3.9 或更高版本。

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
nb run
```

在 `.env` 中至少配置群白名单、当前模型对应的 API Key，以及 OneBot 连接所需配置。模型与提供商映射位于 `my-bot/plugins/multi_llm_chat/model_routes.json`。

## 对话架构

每条 OneBot 群消息会以连续的 `user` 或 `assistant` 消息进入模型上下文。发送时间、发送者、来源、提及对象和回复关系集中放在一份结构化运行时上下文中，与真实消息按顺序一一对应；这些元数据不会混入用户可见正文。

上下文由以下部分组成：

1. 小P稳定的人设、回答原则、自我认知和工具规则。
2. 本轮 `direct` 或 `passive` 参与规则。
3. 当前时间、触发事件和小P近期发言状态。
4. OneBot 群成员身份快照。
5. 有证据的群聊长期记忆。
6. 已压缩的短期滚动摘要。
7. 预算内的近期事件元数据和连续原始消息。

近期消息达到数量或字符预算后，旧消息被工程化分成两路：一份生成有限长度的滚动摘要，另一份生成结构化记忆 patch。压缩完成前不会静默丢弃原始消息；压缩期间新到达的消息也会保留。

小P的长期定位、能力边界和开发者身份由 `my-bot/plugins/multi_llm_chat/self_knowledge.md` 统一维护，并在每次普通聊天请求中作为系统提示词加载。文件路径可通过 `LLM_CHAT_SELF_KNOWLEDGE_FILE` 配置；文件缺失或内容为空时插件不会启动。具体到某一轮能够调用什么，仍以该轮实际提供的工具定义为准。

## 图片理解

收到 OneBot 图片消息后，插件立即从消息段的 `url` 下载图片，校验实际格式和大小，并以内容哈希保存到 `LLM_CHAT_STATE_DIR/media`。聊天事件只保存图片资源引用；构造模型请求时才读取文件并生成 Base64，Base64 不会写入对话状态或原始请求日志。

`LLM_CHAT_IMAGE_UNDERSTANDING` 控制是否向模型发送 Base64 图片。未配置时遵循模型路由默认值：`mimo-v2.5` 开启，DeepSeek 关闭；显式设置为 `true` 或 `false` 可以覆盖模型默认值。无论开关状态如何，消息存储和图片下载行为都完全相同，关闭时模型继续看到原有的 `[图片]` 文本占位。

旧消息成功压缩并移出近期上下文后，其图片资源随即删除；仍被近期事件引用的同内容图片会继续保留。

## 回复去重

普通消息入口和明确提及入口同时保留，分别负责被动参与和立即回复。明确提及会扫描完整的 OneBot 原始消息，因此 `@bot` 位于图片、文字等消息段之间时也会直接回复。

被动参与和答复构造由同一次 Agent 运行完成：需要发言时直接回答或调用工具，不适合发言时调用 `finish_without_reply` 结束本轮。明确提及模式默认必须形成答复；仅当触发事件之后已经出现新的小P回复时，工程层才提供 `finish_without_reply`，由模型判断插入回复是否已经覆盖触发意图。对话压缩和长期记忆维护在后台执行，不阻塞回复链路。

插件通过 NoneBot 的 matcher 上下文和 Bot API 调用回调，记录其他插件针对同一 OneBot `message_id` 发出的群消息。其他非阻塞插件已经回复时，正在等待或生成的被动回复会被取消；发送前还会再次检查。外部插件的实际回复也会作为带来源的 `assistant` 消息进入聊天历史。

## 群身份与记忆

OneBot V11 是群身份的基础数据源。插件使用以下 API：

- `get_group_info`
- `get_group_member_list`
- `get_group_member_info`

群成员快照、近期会话和长期记忆都存放在 `LLM_CHAT_STATE_DIR` 下的 JSON 文件中。该目录是运行时状态，已被 Git 忽略。

可提交的人工身份配置位于 `my-bot/plugins/multi_llm_chat/identity_config.json`：

```json
{
  "version": 1,
  "members": {
    "2997592724": {
      "pinned_aliases": ["PeterAlbus"]
    }
  },
  "groups": {
    "708695087": {
      "append_user_id": true
    }
  }
}
```

`append_user_id` 按群决定近期真实消息里的用户名后是否显式附加 `user_id`。`pinned_aliases` 以 `user_id` 为唯一标识，是跨群生效的人工初始称呼，不会被模型 patch 修改。模型在对话中学习到的称呼保存在对应群的记忆中，同一个人可以拥有多个渐进学习到的称呼；每个称呼都必须引用实际包含该称呼的聊天事件。

长期事实按类别设置有效期并受总数限制。记忆更新只接受结构化 patch，不允许模型重写整个状态文件。

## 模型工具

内置工具包括：

- 获取当前群信息。
- 列出当前群成员。
- 刷新并获取指定群成员。
- 搜索当前群记忆。
- 在隔离容器内执行 CLI 命令。
- 通过 `reply_to_event` 设置最终消息要引用的近期事件。
- 通过 `mention_members` 添加最终消息要 `@` 的当前群成员。

所有工具都使用严格 JSON Schema 和 Pydantic 参数校验，并受最大工具轮次限制；执行外部逻辑的工具还具有执行超时和输出上限。工具只能访问当前群上下文。

`reply_to_event` 和 `mention_members` 只修改当前 Agent 轮次内的回复草稿，不直接调用 OneBot。模型完成工具调用后继续输出普通文本正文，发送层统一拼装 `reply`、`at` 和 `text` 消息段并只发送一次。引用目标必须是近期上下文中保存了 OneBot `message_id` 的事件；`@` 目标必须来自当前群成员快照，且不能是机器人自己。

### 自定义 Python 工具

将 `LLM_CHAT_CUSTOM_TOOLS_MODULE` 设置为一个可导入的 Python 模块。该模块必须提供同步注册函数：

```python
from pydantic import Field

from multi_llm_chat.tools import (
    ToolArguments,
    ToolDefinition,
    ToolRegistry,
)


class WeatherArguments(ToolArguments):
    city: str = Field(min_length=1, max_length=40)


async def query_weather(context, arguments):
    return {"city": arguments.city, "result": "模块返回的数据"}


def register_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolDefinition(
            name="query_weather",
            description="调用项目自定义模块查询天气。",
            arguments_model=WeatherArguments,
            executor=query_weather,
            timeout_seconds=10,
        )
    )
```

`ToolContext` 提供当前 `group_id`、触发用户、OneBot `bot` 和本轮隔离工作目录。自定义 Python 工具运行在机器人进程中，应只注册可信的项目代码。

## CLI 隔离

先构建工具镜像：

```bash
docker build -t peteralbus-nonebot-tool-runner:latest tool_runner
```

每个模型轮次会创建一次性工作目录，并通过 Docker 启动无网络容器。容器采用只读根文件系统、非 root 用户、内存/CPU/PID 限制、移除 Linux capabilities，并且只把本轮工作目录挂载为可写。项目目录、`.env`、宿主网络和 Docker socket 都不会挂载。

`rm`、`rmdir`、`shred`、关机、挂载、进程终止、文件系统格式化以及 `git clean/reset` 等高危命令会在执行前被拒绝。容器超时后会被终止，本轮工作目录随后删除。

## 原始请求日志

模型 raw request 不写入普通应用日志，而是按天追加到 `LLM_CHAT_RAW_REQUEST_LOG_DIR` 下的 JSONL 文件。文件权限为 `0600`，目录权限为 `0700`。每天定时删除超过 `LLM_CHAT_RAW_REQUEST_RETENTION_DAYS` 的文件。

多模态请求中的图片 Base64 会在写入 raw request 前替换成 MIME 类型和编码长度元数据，日志不会保存图片正文。

普通日志只记录 request ID、turn ID、请求类型、模型、耗时、消息数、响应长度和工具调用数，不记录完整提示词或完整记忆内容。

## 每日群聊日报

`multi_llm_chat` 每天按 `Asia/Shanghai` 时区在 10:00 生成一次群聊日报，只发送给 `my-bot/plugins/multi_llm_chat/daily_digest_config.json` 中显式启用的群。当前配置的目标群为 `748245950`。

日报标题固定为“今日日报”。日报直接读取公开来源自身提供的 JSON API、RSS/Atom 或新闻列表页，包括 Open-Meteo、AniList、各游戏官网、PC Gamer、Gematsu、新华网、中国政府网、上海市政府，以及 OpenAI、Google AI、Google DeepMind、Hugging Face 和 Anthropic。单个来源请求或解析失败不会阻止已经获取的内容进入日报，失败详情只写入应用日志，不出现在群聊正文中；天气源失败时省略天气段落。

代码先按固定时间窗口、类别和关键词过滤并去重，再通过一次不带任何工具的模型请求完成候选选择、翻译和摘要。模型只能返回代码提供的候选 `item_id`，发送层根据 `item_id` 拼接真实来源链接。AI 资讯最多一条；日报正文最多七条，并受配置中的字符上限约束。

日报发送成功后以 `source="llm:daily_digest"` 写入群聊历史，和普通模型回复明确区分。

## 测试

```bash
pip install -e ".[dev]"
python -m pytest -q
```

测试覆盖 JSON 原子状态、结构化运行时上下文、压缩并发保留、动态多称呼、记忆过期与上限、OneBot 身份同步、单次被动参与决策、跨插件回复判定、结构化引用与 `@` 回复、严格工具参数、工具循环、CLI 命令约束、日报来源解析与发送约束，以及 raw request 日志清理。
