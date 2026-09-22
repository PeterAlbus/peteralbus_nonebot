# macOS 部署：Miniconda、NoneBot 与 NapCat

本文用于将本仓库与 NapCat 部署到同一台 Mac。目标机已安装 Miniconda，NoneBot 与 NapCat 使用同一个 macOS 用户原生运行，并能够读取同一套图片和 PDF 文件。

部署使用 Python 3.12。`requirements-macos.lock` 固定运行与测试依赖，`start-macos.sh` 在指定 Conda 环境中前台运行机器人，`launchd` 负责登录后的常驻运行。`start.sh` 是已有 Linux 环境的启动脚本。

## 1. 确认环境和准备目录

在目标 Mac 的终端执行：

```bash
uname -m
conda --version
conda info --base
```

记录最后一条命令输出的 Miniconda 根目录。Apple Silicon 应使用 arm64 的 Miniconda、Python 和 QQ；Intel Mac 使用 x86_64 对应版本。

如果终端找不到 `conda`，先用已安装的 Miniconda 的实际绝对路径加载初始化脚本：

```bash
MINICONDA_BASE=""  # 填写实际 Miniconda 根目录
source "$MINICONDA_BASE/etc/profile.d/conda.sh"
conda info --base
```

将仓库放在普通本地目录中。下面两项由部署者填写，后续命令沿用这些变量；重新打开终端后需重新设置。代码和运行数据分别保存，运行数据目录应能够长期保留。

```bash
NB_REPO_DIR=""  # 仓库的绝对路径
NB_DATA_DIR=""  # 运行数据的绝对路径
```

建议使用用户自己拥有的普通目录，例如代码放在用户目录下的 `Services` 中，数据放在 `Library/Application Support/peteralbus-nonebot` 中。避免依赖云盘同步或终端临时授权才能访问的目录。

填写后执行：

```bash
(
set -e
test -n "$NB_REPO_DIR"
test -f "$NB_REPO_DIR/pyproject.toml"
test -n "$NB_DATA_DIR"
cd "$NB_REPO_DIR"
mkdir -p "$NB_DATA_DIR/peteralbus_wife" \
  "$NB_DATA_DIR/whateat_res/eat_pic" \
  "$NB_DATA_DIR/whateat_res/drink_pic" \
  "$NB_DATA_DIR/jm_tasks" \
  "$NB_DATA_DIR/llm_chat_state" \
  "$NB_DATA_DIR/llm_request_logs" \
  "$NB_DATA_DIR/llm_tool_workspaces" \
  "$NB_DATA_DIR/service_logs"
)
```

检查失败时，上面的命令块会退出，先修正变量再重试。含空格或中文的路径需要保留命令中的双引号。

## 2. 创建 Conda 环境并安装锁定依赖

在已能运行 `conda` 的终端执行：

```bash
conda create -n nonebot python=3.12 pip
conda activate nonebot
python --version
python -c 'import platform, sys; print(platform.machine()); print(sys.executable)'
echo "$CONDA_PREFIX"
```

如果已创建该环境，直接激活并确认它使用 Python 3.12。记录 `CONDA_PREFIX` 的绝对路径，稍后写入 `.env.macos`。Conda 环境中的可执行文件应与机器架构一致。

在仓库根目录安装：

```bash
cd "$NB_REPO_DIR"
python -m pip install -r requirements-macos.lock -e '.[dev]'
python -m pip check
python -m pytest -q
```

预期 `pip check` 输出 `No broken requirements found.`，测试全部通过。测试使用模拟消息和临时数据，不启动 QQ 连接。安装遇到错误时先处理安装错误，不继续启动服务。

锁定文件包括 `nb-cli`、本地插件的依赖和以下四个已启用的第三方插件：

| 插件 | 固定版本 | 用途 |
| --- | --- | --- |
| `nonebot-plugin-status` | 0.9.0 | 查看运行状态 |
| `nonebot-plugin-whateat-pic` | 1.5.9 | 今天吃什么、喝什么和图片菜单 |
| `nonebot-plugin-animeres` | 1.0.5 | 动漫资源搜索 |
| `nonebot-plugin-fakemsg` | 0.1.9 | 合并转发消息 |

`nb-cli` 固定为 1.4.2；它与吃什么插件可以共同使用锁定的 `rich==13.9.4`。直接修改单个依赖版本后，应重新解析整套依赖并运行测试。运行机器不需要安装 `uv`，安装上述锁定文件使用 pip 即可。

不要复制 Linux 的 Conda 环境或虚拟环境目录。资源、配置和状态按下文迁移，Python 依赖在目标机重新安装。

## 3. 填写两个本地配置文件

首次部署、文件尚不存在时执行：

```bash
cp .env.macos.example .env.macos
cp .env.example .env
```

已有本地配置时直接编辑，不用示例覆盖。两个实际配置文件均被 Git 忽略。

### 3.1 `.env.macos`：启动环境

```bash
CONDA_BASE=""
CONDA_ENV_PATH=""
```

- `CONDA_BASE`：填写 `conda info --base` 输出的绝对路径。
- `CONDA_ENV_PATH`：填写激活 `nonebot` 后 `echo "$CONDA_PREFIX"` 输出的绝对路径。

此文件由 Bash 读取，保留双引号，填写完整路径。它只负责启动环境；机器人令牌、API Key 和插件配置写入 `.env`。

启动脚本不依赖 `.zshrc` 或当前终端已激活的环境。它加载指定 Miniconda 的初始化脚本，激活指定环境，并使用该环境的 `nb` 和 Python。缺少文件、路径为空、环境缺少可执行文件或激活结果不一致时，会打印错误并退出。

### 3.2 `.env`：连接、资源和模型

按照下表填写。表中的目录名称相对于前面选定的 `NB_DATA_DIR`；写入 `.env` 时请展开为真实绝对路径，不要直接写入 `$NB_DATA_DIR`。

| 配置项 | 部署时填写 |
| --- | --- |
| `DRIVER` | 保持 `~fastapi` |
| `HOST` | 同机部署保持 `127.0.0.1` |
| `PORT` | 默认 `2333`，与 NapCat 配置一致 |
| `ONEBOT_ACCESS_TOKEN` | 与 NapCat 完全一致的访问令牌 |
| `NICKNAME` | 机器人的昵称数组 |
| `PETERALBUS_WIFE_RES` | `peteralbus_wife` 资源目录的绝对路径 |
| `PETERALBUS_WIFE_JM_WORK_DIR` | `jm_tasks` 的绝对路径 |
| `PETERALBUS_WIFE_JM_OPTION_PATH` | 保持 `config.json`，使用仓库中的下载配置 |
| `WHATPIC_RES_PATH` | `whateat_res` 的绝对路径，目录内有 `eat_pic` 和 `drink_pic` |
| `LLM_CHAT_STATE_DIR` | `llm_chat_state` 的绝对路径，迁移已有状态时必须指向数据实际位置 |
| `LLM_CHAT_RAW_REQUEST_LOG_DIR` | `llm_request_logs` 的绝对路径 |
| `LLM_CHAT_CLI_WORKSPACE_DIR` | `llm_tool_workspaces` 的绝对路径 |
| `LLM_CHAT_WHITELIST` | 允许聊天的群号字符串数组，如 `["123456789"]` |
| `LLM_CHAT_MODEL` | `model_routes.json` 中的模型名 |
| `LLM_CHAT_XIAOMI_MIMO_API_KEY` | 使用 MiMo 时填写 |
| `LLM_CHAT_DEEPSEEK_API_KEY` | 使用 DeepSeek 时填写 |
| `APSCHEDULER_CONFIG` | 保持示例中的 `Asia/Shanghai` 时区 |

示例中的机器路径和凭证留空是为了在目标机填写。老婆图片目录和 JM 工作目录为空时，对应命令会报告缺少配置。吃什么插件使用 `WHATPIC_RES_PATH`，请填写迁移后的资源根目录。

本地两个插件的路径都支持 `~`。相对路径分别以各自的插件目录为基准：`peteralbus_wife` 和 `multi_llm_chat`。LLM 的三个运行目录为空时，分别使用 `multi_llm_chat` 下的 `state`、`llm_request_logs`、`tool_workspaces`。迁移部署推荐显式填写绝对路径，以便确认旧数据的位置。第三方吃什么插件的资源目录也使用绝对路径。

JM 的工作目录由 `PETERALBUS_WIFE_JM_WORK_DIR` 指定。每次下载在其中建立独立任务目录，图片和 PDF 均在该任务目录内生成；`config.json` 用于下载参数和目录命名规则。上传成功后任务目录删除，失败任务按 `PETERALBUS_WIFE_JM_FAILED_RETENTION_HOURS` 的配置保留并在后续任务中清理。

`PETERALBUS_WIFE_JM_ALLOWED_GROUPS` 和 `PETERALBUS_WIFE_JM_ALLOWED_USERS` 默认空列表，分别表示不限制群和用户；配置为非空时，两项限制同时生效。`LLM_CHAT_WHITELIST` 为空则不会处理普通群聊消息。

启动前还要检查三个随仓库维护的文件：

- `my-bot/plugins/multi_llm_chat/daily_digest_config.json`：确认启用群、城市和日报发送时间。日报发送群独立于 `LLM_CHAT_WHITELIST`，当前文件已包含启用的群配置。
- `my-bot/plugins/multi_llm_chat/identity_config.json`：确认人工称呼和群显示设置。
- `my-bot/plugins/multi_llm_chat/self_knowledge.md`：保持文件存在且非空，插件启动时会读取。

## 4. 迁移资源与运行数据

先在旧服务器停止 NoneBot，等待正在进行的下载、上传和状态写入结束，再复制最终数据快照。新旧实例不要同时处理同一机器人的消息。这里只迁移文件，不迁移 Linux 环境目录、PID 文件和可执行程序。

按照旧服务器实际 `.env` 中的目录找到源数据，将以下内容复制到 Mac：

| 数据 | Mac 目标位置 | 完整性检查 |
| --- | --- | --- |
| 老婆图片资源 | `PETERALBUS_WIFE_RES` | 角色子目录和图片均存在 |
| 吃什么资源 | `WHATPIC_RES_PATH` | `eat_pic`、`drink_pic` 内有图片 |
| 聊天状态 | `LLM_CHAT_STATE_DIR` | `conversations`、`memories`、`group_rosters`、`media` 一并复制 |
| 原始模型请求日志 | `LLM_CHAT_RAW_REQUEST_LOG_DIR` | 需要保留排查记录时复制 JSONL 文件 |
| 本地 API Key、群配置、OneBot 令牌 | 新 `.env` 和对应配置文件 | 按目标机目录重新填写路径 |

复制状态目录时，目标目录内部应直接出现四个子目录，不要额外套一层源目录名称。聊天 JSON 通过相对媒体标识引用 `media` 中的文件，因此需要完整复制媒体目录。

老婆资源目录至少应包含当前指定角色列表中的五个目录：

```text
peteralbus_wife/
  亚妮艾丝/
  亚尔缇娜/
  鉴纯夏/
  雪之下雪乃/
  流萤/
```

每个目录准备 `.jpg`、`.jpeg`、`.png` 或 `.gif` 图片。普通取图还会从资源根目录下的其他角色目录中随机选择，所有参与选择的目录都应有可用图片。

JM 任务目录和 LLM 工具工作区可以使用新建的空目录。迁移后确认当前 macOS 用户能够读写状态、日志和任务目录；NapCat/QQ 进程能够读取图片和待上传的 PDF。特别检查从 Linux 复制过来的文件所有权和读写权限。

## 5. 安装并连接 NapCat

按 [NapCat 官方 macOS 安装指引](https://napneko.github.io/guide/boot/Shell#napcat-macos-macos安装工具) 安装适配的 QQ 与 NapCat。官方指引要求 macOS 12.0 或以上，并说明安装补丁过程；具体版本组合以该指引和对应发布包为准。

用部署 NoneBot 的同一个 macOS 用户启动 QQ/NapCat 并登录机器人账号，在 NapCat WebUI 中配置 OneBot V11 的 WebSocket 客户端（反向 WebSocket）：

```text
ws://127.0.0.1:2333/onebot/v11/
```

端口必须与 `.env` 的 `PORT` 一致，访问令牌必须与 `ONEBOT_ACCESS_TOKEN` 一致。保存并启用配置。NoneBot 尚未启动时出现连接重试，启动后应恢复连接。连接协议详见 [NoneBot OneBot 配置文档](https://onebot.adapters.nonebot.dev/docs/guide/setup/)。

当前图片发送和 JM 群文件上传都使用本地文件路径。此部署方式要求路径在 NoneBot 与 NapCat 进程中指向同一个文件；验收时需实测图片发送和 PDF 上传。Mac 上的容器部署不在本文范围内。

## 6. 前台启动与功能验收

```bash
cd "$NB_REPO_DIR"
./start-macos.sh
```

脚本不接收参数，前台日志显示在终端。确认所有声明插件均加载成功，且 OneBot 已连接。终端保持打开，使用 `Ctrl+C` 停止。脚本不启用代码自动重载；修改 Python 代码、环境变量或启动时读取的 JSON 配置后需要重启。

在指定测试群按顺序验收：

1. 发送 `/echo hello`，确认基础消息收发。
2. 在聊天白名单群中明确 `@` 机器人，确认模型回复、群成员信息和引用关系。
3. 发送图片并询问内容，确认图片理解；继续聊天后询问先前图片，检查历史图片读取。
4. 使用“今日老婆”命令，确认角色图片能够发送，指定用户的角色选择也正常。
5. 使用“今天吃什么”“今天喝什么”“查看菜单”，确认图片资源和菜单字体正常。
6. 执行一次允许的 JM 下载，确认 PDF 上传到群文件，成功任务目录随后清理。
7. 检查状态、动漫资源搜索、合并转发三个插件的实际使用。
8. 停止并重新启动 NoneBot，确认近期会话和长期记忆仍在。
9. 确认日报启用群和 `Asia/Shanghai` 时间设置，并观察一次实际定时发送。

状态测试、消息发送、搜索和下载需要实际账号或网络，单元测试不能代替这些部署验收。日报各来源、模型 API、QQ 图片源和下载站点需要从目标 Mac 可达。

## 7. 配置登录后常驻运行

前台验收成功后用 `Ctrl+C` 停止前台实例，再安装用户级 LaunchAgent。它在该 macOS 用户登录后启动，并在进程退出后重新拉起。NapCat 的登录和自启动按其部署方式单独设置。

以下命令在已激活的 Conda 环境中运行，使用前文填写的 `NB_REPO_DIR`、`NB_DATA_DIR`。命令生成 plist 时会处理路径中的空格和 XML 特殊字符：

```bash
NB_REPO_DIR="$NB_REPO_DIR" NB_DATA_DIR="$NB_DATA_DIR" python - <<'PY'
import os
import plistlib
from pathlib import Path

repo_value = os.environ["NB_REPO_DIR"]
data_value = os.environ["NB_DATA_DIR"]
if not repo_value or not data_value:
    raise SystemExit("请先填写 NB_REPO_DIR 和 NB_DATA_DIR")
repo = Path(repo_value).expanduser().resolve()
data = Path(data_value).expanduser().resolve()
if not (repo / "start-macos.sh").is_file():
    raise SystemExit("仓库目录不正确")
logs = data / "service_logs"
logs.mkdir(parents=True, exist_ok=True)
destination = Path.home() / "Library/LaunchAgents/com.peteralbus.nonebot.plist"
destination.parent.mkdir(parents=True, exist_ok=True)
job = {
    "Label": "com.peteralbus.nonebot",
    "ProgramArguments": ["/bin/bash", str(repo / "start-macos.sh")],
    "WorkingDirectory": str(repo),
    "RunAtLoad": True,
    "KeepAlive": True,
    "ThrottleInterval": 10,
    "StandardOutPath": str(logs / "stdout.log"),
    "StandardErrorPath": str(logs / "stderr.log"),
    "EnvironmentVariables": {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "PYTHONUNBUFFERED": "1",
        "TZ": "Asia/Shanghai",
    },
}
with destination.open("wb") as file:
    plistlib.dump(job, file)
print(destination)
PY

plutil -lint "$HOME/Library/LaunchAgents/com.peteralbus.nonebot.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.peteralbus.nonebot.plist"
launchctl print "gui/$(id -u)/com.peteralbus.nonebot"
```

`plutil` 应输出 `OK`。`launchctl print` 应能看到运行状态和 PID；同时检查日志中的插件加载、监听端口和 OneBot 连接情况。

常用操作：

```bash
# 查看状态
launchctl print "gui/$(id -u)/com.peteralbus.nonebot"

# 重启已加载的服务
launchctl kickstart -k "gui/$(id -u)/com.peteralbus.nonebot"

# 停止并卸载当前登录会话中的服务
launchctl bootout "gui/$(id -u)/com.peteralbus.nonebot"

# 停止后重新加载
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.peteralbus.nonebot.plist"

# 查看服务日志
tail -n 100 "$NB_DATA_DIR/service_logs/stdout.log" "$NB_DATA_DIR/service_logs/stderr.log"
```

修改 plist 后先 `bootout` 再 `bootstrap`。修改 `.env`、`.env.macos` 或代码后，重启服务即可重新读取。停止服务时使用 `bootout`；单独终止进程会因 `KeepAlive` 再次启动。

LaunchAgent 依赖用户登录会话，退出登录后不会继续运行。需要机器人持续在线时，保持该用户登录，并在 macOS 电源设置中允许连接电源时持续运行，避免系统自动睡眠。系统睡眠期间网络处理和定时任务不会正常执行。

服务日志位于 `service_logs`，与模型请求 JSONL 日志分开。当前 LaunchAgent 配置只负责写日志，需按实际容量维护服务日志文件。模型请求日志仍按 `LLM_CHAT_RAW_REQUEST_RETENTION_DAYS` 清理。

## 8. 排查部署问题

| 现象 | 检查位置与处理 |
| --- | --- |
| 启动脚本提示 Conda 路径为空或找不到文件 | 核对 `.env.macos` 与 `conda info --base`、`CONDA_PREFIX` 的输出 |
| 找不到环境中的 `nb` | 激活目标环境，重新执行锁定依赖安装，确认环境中有 `bin/nb` |
| 插件加载失败或出现依赖错误 | 查看完整错误日志；执行 `python -m pip check`，确认安装的是锁定文件对应的 Python 3.12 环境 |
| NoneBot 正常启动但 NapCat 未连接 | 检查反向 WS 客户端是否启用、路径是否为 `/onebot/v11/`、端口和令牌是否一致 |
| 普通群聊没有响应 | 检查 `LLM_CHAT_WHITELIST`、模型名称、对应提供商 API Key；被动消息也可能由模型决定不回复 |
| 老婆图片报目录错误 | 检查 `PETERALBUS_WIFE_RES`、五个指定角色目录及图片文件；NapCat 也需要能够读取图片 |
| 吃什么或菜单找不到图片 | 检查 `WHATPIC_RES_PATH` 内的 `eat_pic`、`drink_pic`，并确认用户有读取权限 |
| JM 下载成功但上传失败 | 检查 NapCat 是否可读取实际 PDF 路径、群文件权限、上传超时和配置的大小限制；失败文件位于 JM 任务目录 |
| 机器人没有原有记忆或历史图片无法读取 | 核对 `LLM_CHAT_STATE_DIR` 是否直接包含四个状态子目录，确认 `media` 一并迁移 |
| 终端能启动，LaunchAgent 启动失败 | 检查 `service_logs/stderr.log`、plist 中的仓库路径、`.env.macos` 和 macOS 对目录的访问权限 |
| 端口已占用 | 执行 `lsof -nP -iTCP:2333 -sTCP:LISTEN`，确认是否同时运行了前台实例和 LaunchAgent |

## 9. 更新依赖与验证范围

运行时依赖以 `pyproject.toml` 为声明来源，macOS Python 3.12 的完整版本集合以 `requirements-macos.lock` 为部署输入。更新依赖时，由维护者使用 `uv` 重新生成并在独立环境验证：

```bash
uv pip compile pyproject.toml --extra dev --python-version 3.12 --python-platform macos \
  --no-annotate --output-file requirements-macos.lock
```

需要升级锁定版本时在生成命令中加入 `--upgrade`；重新运行测试、全部插件加载检查和涉及功能的部署验收后，再使用新锁定文件部署。

仓库修改验证使用 macOS arm64 的独立 Python 3.12 环境，覆盖锁定依赖安装、自动测试、插件加载和本地资源处理。目标机 Miniconda 激活、LaunchAgent 登录自启、NapCat/QQ 登录和实际消息发送仍按本文步骤现场验收；Intel Mac 也需在对应架构上完成安装与验收。

参考：[Conda 环境管理](https://docs.conda.io/projects/conda/en/stable/user-guide/tasks/manage-environments.html)、[Apple LaunchAgent 文档](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html)。
