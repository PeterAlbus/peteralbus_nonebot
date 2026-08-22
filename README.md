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

每条 OneBot 群消息会以独立的 `user` 或 `assistant` 消息进入模型上下文，并携带发送时间。聊天记录不会作为一段文本混入提示词。

上下文由以下部分组成：

1. 小P的人设和工具规则。
2. 当前时间。
3. OneBot 群成员身份快照。
4. 有证据的群聊长期记忆。
5. 已压缩的短期滚动摘要。
6. 预算内的近期原始消息。

近期消息达到数量或字符预算后，旧消息被工程化分成两路：一份生成有限长度的滚动摘要，另一份生成结构化记忆 patch。压缩完成前不会静默丢弃原始消息；压缩期间新到达的消息也会保留。

## 图片理解

收到 OneBot 图片消息后，插件立即从消息段的 `url` 下载图片，校验实际格式和大小，并以内容哈希保存到 `LLM_CHAT_STATE_DIR/media`。聊天事件只保存图片资源引用；构造模型请求时才读取文件并生成 Base64，Base64 不会写入对话状态或原始请求日志。

`LLM_CHAT_IMAGE_UNDERSTANDING` 控制是否向模型发送 Base64 图片。未配置时遵循模型路由默认值：`mimo-v2.5` 开启，DeepSeek 关闭；显式设置为 `true` 或 `false` 可以覆盖模型默认值。无论开关状态如何，消息存储和图片下载行为都完全相同，关闭时模型继续看到原有的 `[图片]` 文本占位。

旧消息成功压缩并移出近期上下文后，其图片资源随即删除；仍被近期事件引用的同内容图片会继续保留。

## 回复去重

普通消息入口和明确提及入口同时保留，分别负责被动参与和立即回复。明确提及会扫描完整的 OneBot 原始消息，因此 `@bot` 位于图片、文字等消息段之间时也会直接回复。

插件通过 NoneBot 的 matcher 上下文和 Bot API 调用回调，记录其他插件针对同一 OneBot `message_id` 发出的群消息。其他非阻塞插件已经回复时，LLM 会在判断前、模型执行前和最终发送前停止回复。外部插件的实际回复也会作为带来源的 `assistant` 消息进入聊天历史。

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

每个工具使用严格 JSON Schema 和 Pydantic 参数校验，并具有执行超时、输出上限和最大工具轮次。工具只能访问当前群上下文。

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

## 测试

```bash
pip install -e ".[dev]"
python -m pytest -q
```

测试覆盖 JSON 原子状态、上下文角色和时间、压缩并发保留、动态多称呼、记忆过期与上限、OneBot 身份同步、跨插件回复判定、严格工具参数、工具循环、CLI 命令约束以及 raw request 日志清理。
