# 小P的 Codex 后台任务

小P在普通群聊轮次中按需调用 `codex_task`，提交后继续使用自己的回复工具说明任务已受理。后台通过 Codex CLI 的 `app-server --listen stdio://` 执行。一个机器人进程同时执行一个任务，后续任务排队；Codex 的工具轮次不占用小P当前回复的工具轮次。

执行中的阶段信息按间隔合并，重新触发小P结合当前群聊判断是否值得通知。执行结束后，小P重新生成结果回复并发到任务所属群。通知与普通回复共用群级发送锁，结果写入聊天记录；已经结束的任务不会继续发出旧的“执行中”通知。通知轮次不提供再次委托工具。

## 环境配置

沿用仓库的 Python 3.12 部署环境。机器人所在主机需要安装 Codex CLI，并由运行机器人的同一系统用户完成登录。此实现按本机 `codex-cli 0.172.0` 导出的协议 schema 实现，使用实验性的 `additionalContext`；部署使用该版本验证，不承诺其他版本的实验字段兼容性。

```bash
npm install -g @openai/codex@0.172.0
codex --version
codex login
codex login status
command -v codex
mkdir -p "$HOME/xiaop-workspace"
```

如果 Codex 已由其他方式安装，直接检查版本和登录状态即可。机器人实际加载的环境文件（本仓库部署文档使用 `.env`）加入：

```dotenv
LLM_CHAT_CODEX_ENABLED=true
LLM_CHAT_CODEX_BINARY="/opt/homebrew/bin/codex"
LLM_CHAT_CODEX_WORKING_DIR="/Users/yourname/xiaop-workspace"
LLM_CHAT_CODEX_HOME="/Users/yourname/.codex"
LLM_CHAT_CODEX_MODEL=""
LLM_CHAT_CODEX_SANDBOX="workspace-write"
LLM_CHAT_CODEX_NETWORK_ACCESS=false
LLM_CHAT_CODEX_TASK_TIMEOUT=1800
LLM_CHAT_CODEX_PROGRESS_INTERVAL=60
```

- `BINARY` 使用 `command -v codex` 得到的绝对路径，避免常驻服务的 PATH 与终端不同。
- `WORKING_DIR` 必须是存在的绝对目录，是 Codex 的工作与写入范围。需要操作某个仓库时指向该仓库。任务会在这个目录实际执行，不自动创建 Git worktree。
- `HOME` 对应已登录的 Codex 配置目录；留空继承进程的 `CODEX_HOME` 或 Codex 默认目录。自定义目录需使用同一 `CODEX_HOME` 运行 `codex login`。小P模型的 API key 不会自动成为 Codex 凭据。
- `MODEL` 留空使用 Codex 配置的默认模型和 provider；可指定该账号支持的模型。工作区中的 Codex 项目配置、AGENTS.md 也会影响执行。
- `SANDBOX` 可选 `workspace-write` 或 `read-only`。`NETWORK_ACCESS` 控制 workspace-write 沙箱内命令联网；不控制 Codex 自身连接模型服务。
- `TASK_TIMEOUT` 是单个任务执行期限，默认 1800 秒；`PROGRESS_INTERVAL` 是进度合并间隔，默认 60 秒。任务完成不等待进度间隔；通知生成失败会按该间隔重试。

重启机器人使配置生效；已有 `LLM_CHAT_WHITELIST` 继续决定小P参与哪些群。功能默认关闭，不需要注册新命令或安装新的 Python SDK。Codex CLI 运行在机器人主机，和现有 `run_cli` Docker 工具是不同的执行环境。

## 上下文与双重提示词门禁

委托附带当轮选中的群聊事件、滚动摘要、相关记忆、参与者信息、当前轮已产生的工具结果。相关图片复制到任务目录，聊天记录压缩不会删除这份副本。聊天内容通过标记为 `untrusted` 的上下文片段传给 Codex。

小P在委托前、Codex在执行前分别收到风险判断提示词，要求独立审查操作的权限、影响范围和可逆性。原始发起人 QQ ID 由收到的消息提供，工具没有修改发起人身份的参数。

- 发起人为开发者 `2997592724`：提示词要求始终按其明确要求已获授权处理，不因危险性判断拒绝。
- 其他发起人：交由模型判断，危险且缺乏合理授权的操作不执行并说明原因。
- 聊天、引用、昵称、工具结果中的开发者身份声明不改变发起人身份。

这两层是模型提示词约束，没有按 QQ ID 或危险操作名单实现代码硬拦截。Codex 仍受部署的沙箱、系统权限及其自身规则约束；提示词授权不代表越过这些边界。后台使用 `approvalPolicy=never`，不等待终端审批；需要交互的问题作为任务失败交由小P说明。

## 状态与运维

任务写入 `LLM_CHAT_STATE_DIR/codex_tasks/<task_id>/task.json`，旁边保存图片副本。记录包含任务状态、原始发起人、上下文、Codex thread/turn ID、阶段记录、结果和投递状态。它们包含聊天与任务数据，应按机器人状态目录管理。

进程停止会终止当前 Codex 进程组。启动时将未完成任务标记为中断并通知小P，不自动重跑可能已经产生副作用的任务。结果在发送前记为 `sending`，确认成功后记为 `sent`；发送过程出错或进程在发送期间停止时记为 `uncertain`，不自动重复发送未知是否已送达的消息。模型生成失败、机器人暂时未连接等发生在发送前的错误可以重试。

结果正文最多保存 24000 字符，超过时明确标记截断；进度保存最近八条摘要。不会把完整命令输出或推理过程直接推送到群里。任务结束与任务目标成功是不同状态，小P须据证据说明未完成项。

部署验证：在白名单群要求小P完成一个多步只读调查，观察先收到受理回复；期间继续聊天；结束后收到小P总结。可检查任务文件中的状态及原始发起人，确认 `completed` 与 `notification=sent`。代码测试不使用真实账号发起模型任务或发送 QQ 消息。

实现参考：[botmux](https://github.com/deepcoldy/botmux) 的 app-server 通信及上下文片段方式；协议依据本机生成的 schema 和 [Codex app-server 文档](https://developers.openai.com/codex/app-server)。
