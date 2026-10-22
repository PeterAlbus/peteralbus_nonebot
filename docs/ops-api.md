# macOS 运维 API

该程序是独立的 FastAPI 管理服务，使用仓库当前 Conda 环境中的 Python 运行。它不会作为 NoneBot 插件加载，因此重启 NoneBot 时管理 API 仍可用。服务只允许绑定 `127.0.0.1` 或 `::1`，所有接口要求固定 Bearer Token。

## 配置与启动

```bash
cp ops-api.env.example .ops-api.env
openssl rand -hex 32
```

把随机输出填入 `.ops-api.env` 的 `OPS_API_TOKEN`，并填写机器人 QQ 号 `OPS_NAPCAT_ACCOUNT`。再确认 `OPS_NONEBOT_LOG_FILE`、NapCat 日志路径和 `.env.macos` 中的 Conda 路径。`.ops-api.env` 已加入 `.gitignore`，不要把 Token 提交到仓库。

手动前台启动：

```bash
./start-ops-api-macos.sh
```

默认监听 `127.0.0.1:8765`。启动时会拒绝短 Token 和非 loopback 绑定地址。当前 Mac 已安装 `com.peteralbus.ops-api` 登录 LaunchAgent，登录时启动并在进程退出后拉起；日志写入 `~/Documents/napcat/service_logs/ops-api.{stdout,stderr}.log`。

## 接口

认证头格式：`Authorization: Bearer <OPS_API_TOKEN>`。管理 Token 不接受 URL 参数，也不会随配置查询返回。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/v1/system` | macOS 版本、运行时间、CPU 负载、内存估算、磁盘占用 |
| `GET` | `/api/v1/nonebot` | LaunchAgent、NoneBot HTTP 监听和最近 OneBot 连接状态 |
| `GET` | `/api/v1/napcat` | QQ/NapCat 进程、登录状态估算、WebUI/API 端口和 OneBot 状态 |
| `POST` | `/api/v1/napcat/restart` | 仅重启配置账号的 QQ/NapCat，并等待服务端口启动 |
| `POST` | `/api/v1/nonebot/restart` | 使用 `launchctl kickstart -k` 重启 NoneBot LaunchAgent |
| `GET` | `/api/v1/nonebot/config` | 读取 `.env.example` 中允许管理的 NoneBot 配置 |
| `PATCH` | `/api/v1/nonebot/config` | 修改允许管理的 `.env` 字段，返回 `restart_required: true` |
| `GET` | `/api/v1/nonebot/model-routes/models` | 读取 `OPS_MODEL_ROUTES_FILE` 中完整的 `models` |
| `PUT` | `/api/v1/nonebot/model-routes/models` | 全量替换模型路由文件中的 `models`，保留 `providers`，返回 `restart_required: true` |
| `GET` | `/api/v1/llm/balances` | 查询 DeepSeek、火山账号余额，分别返回查询状态 |
| `GET` | `/api/v1/llm/providers/volc_ark/usage` | 查询火山方舟按日推理用量 |
| `GET` | `/api/v1/llm/usage` | 查询小P自身记录的 Token 用量 |

`.env` 中的 API Key、OneBot Access Token 等敏感字段只返回 `configured` 状态；PATCH 可以设置或清空它们，响应也不会回显密钥。配置修改只接受模板内字段和少量显式可选字段，不接受任意环境变量；`HOST` 只允许 loopback。空的密钥字段传 `null` 可清空。修改配置后需单独调用 NoneBot 重启接口，避免未经检查自动重启。

示例：

```bash
export OPS_TOKEN='从 .ops-api.env 读取的固定 Token'
curl -H "Authorization: Bearer $OPS_TOKEN" \
  http://127.0.0.1:8765/api/v1/nonebot

curl -X PATCH http://127.0.0.1:8765/api/v1/nonebot/config \
  -H "Authorization: Bearer $OPS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"LLM_CHAT_MODEL":"doubao-seed-2-1-lite-260915"}'
```

NapCat 重启会先查找命令行中明确带有 `-q <OPS_NAPCAT_ACCOUNT>` 的 QQ 进程，只结束该机器人实例，然后通过 macOS `open -a QQ.app --args --no-sandbox -q <账号>` 重新启动。不会用 `pkill QQ` 结束其他 QQ 账号。若进程命令不符合已配置账号，接口会拒绝重启。

## 更新模型路由

在运维服务的 `.ops-api.env` 中指定目标文件，并重启运维服务加载路径：

```dotenv
OPS_MODEL_ROUTES_FILE=/Users/peteralbus/Documents/napcat/llm_config/model_routes.json
```

支持绝对路径、`~` 和相对于仓库根目录的路径。该设置独立于 NoneBot `.env` 的 `LLM_CHAT_ROUTES_FILE`；修改 `.env` 不改变接口的写入目标。要让机器人使用接口管理的路由文件，需要将两者配置为同一个文件。未配置目标路径、文件不存在、文件内容损坏或文件无法写入时返回 `503`。

`PUT /api/v1/nonebot/model-routes/models` 的请求体只能包含 `models`。它会全量替换模型集合：新增键表示新增模型，同名键替换整个模型定义，未提交的模型会被删除，`{"models":{}}` 会清空模型集合。`providers` 及其他顶层数据保留原值；请求中携带 `providers`、路径或其他顶层字段会返回 `422`。

每个模型必须有非空名称，定义中必须用 `provider` 引用文件中已存在的供应商。可选字段为布尔值 `supports_image_understanding` 和对象 `create_params`，其他模型字段会被拒绝。`create_params` 保存模型调用参数，接口不验证远端模型是否可用。

修改前可通过 GET 获取当前文件中的完整模型集合：

```bash
curl -H "Authorization: Bearer $OPS_TOKEN" \
  https://peteralbus.com/ops-api/api/v1/nonebot/model-routes/models
```

响应为 `{"models":{...}}`，只包含模型集合，可编辑后直接作为 PUT 请求体提交。每次请求都读取 `OPS_MODEL_ROUTES_FILE` 指定文件的当前内容；它表示磁盘配置，不代表运行中的 NoneBot 已加载该版本。GET 同样要求 Bearer Token；路径未配置、文件不存在、损坏或不可读时返回 `503`。

下面的示例会将模型集合替换为一个模型，执行前应在请求中准备好需要保留的完整集合：

```bash
curl -X PUT https://peteralbus.com/ops-api/api/v1/nonebot/model-routes/models \
  -H "Authorization: Bearer $OPS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"models":{"doubao-seed-2-1-pro-260915":{"provider":"volc_ark","supports_image_understanding":true,"create_params":{"extra_body":{"thinking":{"type":"disabled"}}}}}}'
```

成功响应包含保存后的 `models` 和 `restart_required: true`。写入通过同目录临时文件原子替换，并保留文件权限；接口拒绝写入符号链接。保存后需单独调用 NoneBot 重启接口才能加载路由；还需确保 `.env` 中选中的 `LLM_CHAT_MODEL` 存在于提交的模型集合中。

## 余额与 Token 用量

这三个接口均使用现有 Bearer Token 鉴权。

### 供应商账号查询

DeepSeek 余额使用 `OPS_NONEBOT_ENV_FILE` 中的 `LLM_CHAT_DEEPSEEK_API_KEY` 调用官方 `/user/balance`，返回币种、总余额、赠送余额、充值余额。火山余额是整个云账号的余额，通过费用中心 `QueryBalanceAcct` 查询。

火山余额与方舟历史用量需要独立的 AK/SK，在 `.ops-api.env` 中配置并重启运维服务：

```dotenv
OPS_VOLC_ACCESS_KEY=填写AK
OPS_VOLC_SECRET_KEY=填写SK
OPS_VOLC_PROJECT_NAME=default
```

对应权限为 `billing:QueryBalanceAcct` 和 `ark:GetInferenceUsage`。方舟推理 API Key 不能代替 AK/SK。余额接口返回 `providers` 数组，各供应商的 `status` 为 `ok`、`not_configured`、`unsupported` 或具体查询错误（如 `upstream_error`、`upstream_timeout`）。MiMo 的余额查询标记为 `unsupported`，原因是尚无已确认的公开余额 API；不将不可查询状态表示为零余额。

```bash
curl -H "Authorization: Bearer $OPS_TOKEN" \
  https://peteralbus.com/ops-api/api/v1/llm/balances

curl -G -H "Authorization: Bearer $OPS_TOKEN" \
  https://peteralbus.com/ops-api/api/v1/llm/providers/volc_ark/usage \
  --data-urlencode start_date=2026-10-01 \
  --data-urlencode end_date=2026-10-22 \
  --data-urlencode api_key_id=apikey-资源ID
```

火山用量查询必填 `start_date`、`end_date`（北京时间日期，`YYYY-MM-DD`），按 `Day` 粒度向官方查询。可选 `api_key_id` 映射官方 `ApikeyID` 过滤字段，必须填写 API Key 的资源 ID，而非密钥；可选 `model` 映射官方 `ModelName`。结果保留官方的 `Fields`、`Data`、`DataCount` 列定义与数据。未限定 API Key 时，查询范围是配置账号和项目下的用量，不应视为小P专属消耗。AK/SK 未配置返回 `503`，上游错误返回 `502`，超时返回 `504`。

### 小P自身用量与工具

小P启动时在 `LLM_CHAT_STATE_DIR` 下创建 `usage.sqlite3`。每次模型调用在发送前记录一次尝试，收到响应后保存官方返回的 `usage`；用量按供应商、模型、请求类型和北京时间日期汇总。数据库独立于原始请求日志，不随日志清理删除。持久化失败会使本次调用失败，避免无记录地继续消耗。

运维服务通过独立配置指向同一数据库：

```dotenv
OPS_LLM_USAGE_DB_FILE=/Users/peteralbus/Documents/napcat/llm_chat_state/usage.sqlite3
```

运维接口只读数据库；未配置或数据库不可读返回 `503`，不会创建空库。新部署需先启动机器人创建数据库，再查询。

```bash
curl -G -H "Authorization: Bearer $OPS_TOKEN" \
  https://peteralbus.com/ops-api/api/v1/llm/usage \
  --data-urlencode start_date=2026-10-01 \
  --data-urlencode end_date=2026-10-22
```

日期范围包含首尾两天。省略日期时查询北京时间今天；仅传结束日期时查询该天。可选筛选参数为 `provider`、`model`、`request_type`，均精确匹配。响应包含 `totals` 和按日期、供应商、模型、请求类型分组的 `breakdown`，以及 `tracking_started_at`（统计启用时间）、`as_of`（查询截止时间）。

统计口径：

- 包含所有群的聊天，以及会话压缩、记忆维护、每日摘要等经过模型调用层的请求。被屏蔽或最终未采用的回复，其已返回用量仍然计入。
- `requests` 是应用层调用尝试数，同一次逻辑请求的每次应用层重试分别计数。`unknown_usage_requests` 是输入、输出或总量不完整的尝试数；失败、取消或进程中断且未收到用量的请求不按零消耗处理。
- `pending_requests` 表示尚未记录结果的尝试（包括进程中断后留下的记录），`failed_requests` 表示明确报错的尝试。SDK 内部重试的独立响应不可见；成功响应披露的重试次数放入 `sdk_retries_without_usage`，其独立用量未计入。当 SDK 最终抛错且不披露重试次数时，也无法得知内部重试数量。
- Token 各字段只合计已知值；没有已知值时返回 `null`。存在未知用量或 SDK 内部重试时，合计只是已观测到的用量，不是完整账单。
- `cached_tokens` 是输入 Token 子项，`reasoning_tokens` 是输出 Token 子项，两者不能再加到 `total_tokens` 上。不根据 Token 推算费用。
- 统计从数据库启用时开始，不回填历史日志或供应商账号用量；返回磁盘已记录的数据，不包含尚未生成的本次最终回答。

小P新增工具 `get_token_usage`，使用同一统计逻辑，接受上述五个可选参数（空值表示不指定）。例如可以询问“小P，你今天用了多少 Token？”或“最近七天哪些模型消耗最多？”。工具查询小P全局的汇总用量，不返回群聊内容或账号余额，也不需要运维管理 Token 或火山 AK/SK。

小P还提供无参数工具 `get_balance`，例如可以询问“小P，你现在还有多少余额？”。供应商由运行中 `LLM_CHAT_MODEL` 对应的已加载路由 `provider` 决定，不按模型名称推测，也不读取尚未重启加载的路由改动。火山接入的 DeepSeek 模型会查询火山余额。

- `deepseek`：使用当前路由 `api_key_field` 指向的运行时密钥，查询 DeepSeek 账号余额。
- `volc_ark`：每次查询从仓库根目录 `.ops-api.env` 读取 `OPS_VOLC_ACCESS_KEY` 和 `OPS_VOLC_SECRET_KEY`，查询火山整个云账号余额。
- `xiaomi_mimo`：返回 `status: unsupported`，不发出余额网络请求。

工具返回当前模型、供应商、查询状态，成功时包含 `scope: account` 和 `balance`。未配置返回 `not_configured`，上游失败返回对应错误状态。机器人和运维服务共用官方查询模块；机器人不需要加载运维服务配置对象，不需要管理 Token，也不依赖运维 HTTP 服务。若使用外部 `LLM_CHAT_SELF_KNOWLEDGE_FILE`，部署时应同步仓库自我认知文档中的余额查询能力说明。

官方依据：[DeepSeek 余额](https://api-docs.deepseek.com/api/get-user-balance/)、[火山余额](https://docs.volcengine.com/docs/BillingCenter/QueryBalanceAcct-Queryuseraccountbalanceinformation?lang=zh)、[方舟推理用量](https://docs.volcengine.com/docs/ark/get-inference-usage-api?lang=en)、[火山签名](https://docs.volcengine.com/docs/APISignatureCallGuide/Signaturemethod-9?lang=zh)、[MiMo 响应 usage](https://mimo.mi.com/docs/en-US/api/chat/openai-api)。

## SSH 隧道与公网 HTTPS

管理 API 继续只监听 Mac 本机。当前 `com.peteralbus.napcat-tunnel` LaunchAgent 保留 NapCat WebUI 的 `16099` 转发，并新增 `127.0.0.1:18765 -> 127.0.0.1:8765`。服务器端隧道只绑定 loopback。

服务器 Nginx 的 `www.peteralbus.com:443` 虚拟主机将 `/ops-api/` 转发到 `127.0.0.1:18765`。公网 API 地址为 `https://www.peteralbus.com/ops-api/api/v1/...`，仍需 Bearer Token。可用 `https://www.peteralbus.com/ops-api/api/v1/system` 检查接口。

服务器的 `8085` 当前由现有 EarthQuick 网站占用，且使用明文 HTTP；该站点保持不变。运维 API 通过已有 HTTPS 443 入口提供，避免在公网明文传输管理 Token。

内存 `used_bytes` 根据 macOS `vm_stat` 的空闲、inactive 和 speculative 页估算；磁盘使用率来自仓库所在卷。NapCat 登录状态结合 OneBot 连接和近期 NapCat 日志判断，因此若日志已轮转或 QQ 没有消息活动，状态可能显示 `unknown`。
