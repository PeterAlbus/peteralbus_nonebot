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

## SSH 隧道与公网 HTTPS

管理 API 继续只监听 Mac 本机。当前 `com.peteralbus.napcat-tunnel` LaunchAgent 保留 NapCat WebUI 的 `16099` 转发，并新增 `127.0.0.1:18765 -> 127.0.0.1:8765`。服务器端隧道只绑定 loopback。

服务器 Nginx 的 `www.peteralbus.com:443` 虚拟主机将 `/ops-api/` 转发到 `127.0.0.1:18765`。公网 API 地址为 `https://www.peteralbus.com/ops-api/api/v1/...`，仍需 Bearer Token。可用 `https://www.peteralbus.com/ops-api/api/v1/system` 检查接口。

服务器的 `8085` 当前由现有 EarthQuick 网站占用，且使用明文 HTTP；该站点保持不变。运维 API 通过已有 HTTPS 443 入口提供，避免在公网明文传输管理 Token。

内存 `used_bytes` 根据 macOS `vm_stat` 的空闲、inactive 和 speculative 页估算；磁盘使用率来自仓库所在卷。NapCat 登录状态结合 OneBot 连接和近期 NapCat 日志判断，因此若日志已轮转或 QQ 没有消息活动，状态可能显示 `unknown`。
