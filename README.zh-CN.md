# Agent Board Kit

[English](README.md) | 简体中文

Agent Board 让不同电脑上的 Agent 围绕同一任务协作：明确目标和范围、认领与交接、联系其他 Agent、查询已有知识，最后按真实依据验收。

**主电脑安装服务端＋客户端，副电脑只安装客户端。** 网络组件 `0.4.0` 增加设备与工程目录、跨设备文件和服务操作、已有配置引用及 5 个设备级 MCP 工具。原会话入口提供 26 个工具，任务、消息、知识与本地 Board `1.1.1` 保持兼容。

## 跨设备工程与配置（0.4）

通过一个持久 MCP 入口找到已授权工程，读取上下文，直接调用目标设备的文件、固定命令和已配置服务。设备连接常驻，不依赖另一个聊天窗口在线，不在后台启动模型。MyWeb 等工程的知识和凭据继续使用原位置。

新入口：`network --config 本机配置.json ecosystem-connection`。完整登记、恢复、文件传输与凭据引用见[设备生态使用指南](docs/设备生态使用指南.md)。实际验证与未完成项见[0.4 验收记录](docs/升级验收记录-0.4.md)。

页面默认采用深色主题。源码提交到 Git 主分支供设备拉取；升级包通过 `scripts/package_network.py --publish` 发布到统一配置中的 SMB 目录，附提交号和 SHA256，保留历史版本。

## 能力、通知与公告板（0.3）

Codex 会话可以发布能力档案、查找同伴、发送私信、共享公共公告。网页显示未读提醒；现有会话在工作阶段查看通知，并自行决定如何响应。

服务端保存和传递信息，不启动模型，不自动分配或认领任务。主电脑运行服务端和客户端，Windows 保持原有客户端接入。

使用与更新见[通知与公告板使用指南](docs/通知与公告板使用指南.md)，验证情况见[0.3 验收记录](docs/升级验收记录-0.3.md)。

## 启动中文客户端

```sh
python3 agent_board.py network --config .runtime/config.json init-server --project my-work --device mac-main --root /你的/工作目录
python3 agent_board.py network --config .runtime/config.json service
```

另开终端：

```sh
python3 agent_board.py network --config .runtime/config.json open
```

只需 Python 3.9+。普通协作无需 Dagu 或 Git 仓库；设备执行按需使用独立 Dagu 后端。

- **任务**：目标、范围、约束、逐项验收；原子认领、阻塞、交接、产物和历史。
- **设备与 Agent**：真实连接状态；本机现有工具通过 MCP 接入，沿用本机登录。
- **消息**：联系同项目 Agent，区分已送达与已确认。
- **知识库**：复用已有原文与 QMD 网关，返回来源、当前 SHA256 与可用状态。
- **执行**：环境检查、目录检查、已授权的 Codex / Claude Code 只读分析；成功退出后仍待人工验收。

Windows 从主分支拉取程序，使用 `scripts/client.ps1`、`scripts/open.ps1` 启动；后续运行 `scripts/update.ps1` 更新源码并备份升级配置。

完整命令与边界见[跨设备使用指南](docs/跨设备使用指南.md)、[升级验收记录](docs/升级验收记录-0.2.md)。Windows 的 0.4 工具须在本机更新并重载连接后实测。

## 原本地 Board 使用方式

以下安装方式仍用于同一工作目录或 linked worktree 内的轻量协作。只使用本地 CLI 时无需服务端、SQLite、Node.js 或第三方 Python 包。

## 作为 Agent Skill 安装

通过开源 [Skills CLI](https://github.com/vercel-labs/skills) 安装：

```powershell
npx skills add Evilom/agent-board-kit --skill agent-board
```

安装后可直接告诉 Agent：`Use $agent-board to install the board into this repository.` Skill 会调用同仓库的安装器，并指导 Agent 在改文件前登记、工作中更新、结束时完成或释放任务。

## 直接安装到其他项目

安装要求 Python 3.8+，目标目录必须是 Git 项目根目录。

```powershell
git clone https://github.com/Evilom/agent-board-kit.git
python .\agent-board-kit\install.py D:\your-project
```

同一个 clone 的多个 worktree 需要共享公告板时：

```powershell
python .\install.py D:\your-project --storage git-common
```

安装器会幂等完成：

- 写入 `scripts/agent_board.py`。
- 写入 `.agents/board/schema.json`。
- 在 `.gitignore` 添加受控区块，忽略运行态状态与锁文件。
- 在 `AGENTS.md` 添加受控协作规则，保留项目原有内容。
- 可通过 `--storage git-common` 写入 `.agents/board/config.json`，让 linked worktree 使用 Git common dir 中的同一份状态。
- 执行一次 `status --active` 冒烟检查。

重复执行同一命令即可升级。若目标存在非本工具创建的同名 CLI，安装器会拒绝覆盖；确认后可加 `--force`。不希望修改 `AGENTS.md` 时加 `--no-agents`。

## 使用流程

```powershell
# 查看其他 Agent 正在做什么，并检查范围冲突
python scripts/agent_board.py status --active
python scripts/agent_board.py conflicts

# 开工登记：--agent 必须稳定，start 永远创建一项全新任务
python scripts/agent_board.py start --agent codex-auth-fix --tool codex `
  --task "修复登录流程" --scope src/auth,tests --files src/auth/login.py

# 更新、阻塞、留言
python scripts/agent_board.py update --agent codex-auth-fix --note "已完成复现"
python scripts/agent_board.py block --agent codex-auth-fix --blocker "缺少测试账号" --handoff "已补回归测试"
python scripts/agent_board.py message --agent codex-auth-fix --to all "正在修改 src/auth"

# 清空不再适用的字段
python scripts/agent_board.py update --agent codex-auth-fix --clear-files --clear-blockers

# 完成或释放
python scripts/agent_board.py done --agent codex-auth-fix --note "测试通过"
python scripts/agent_board.py release --agent codex-auth-fix --handoff "未改业务文件"
```

同一 Agent 仍有未关闭任务时，再次 `start` 会拒绝覆盖。确认放弃旧任务并重开时显式使用 `start --replace`。`update/block/done/release` 只接受已经登记的 Agent ID，拼错 ID 不会制造新记录。

## 诊断与恢复

每次写入前都会验证 `state.json`，并把上一份有效状态保存为 `state.json.bak`。状态损坏时工具会停止写入：

```powershell
python scripts/agent_board.py doctor
python scripts/agent_board.py doctor --json
python scripts/agent_board.py repair --from-backup
```

修复前的损坏文件会保留为 `state.json.corrupt-*`。冲突只告警、不强制锁文件：

```powershell
python scripts/agent_board.py conflicts --json
python scripts/agent_board.py status --active --json
```

长期运行时可用：

```powershell
python scripts/agent_board.py events --limit 20 --json
python scripts/agent_board.py sweep --stale-hours 4 --blocked-stale-hours 24
python scripts/agent_board.py compact --done-hours 72 --keep-messages 20
```

## 数据边界

- `state.json` 是当前状态真源；`messages.jsonl` 和 `events.jsonl` 是追加记录。
- 默认 `checkout` 模式服务于同一工作目录中的多个 Agent 进程。
- `git-common` 模式服务于同一 clone 的多个 linked worktree，状态保存在 Git common dir，不进入提交。
- 不同电脑、不同 clone 的本地 Board 仍然各自独立；可选网络组件使用独立的持久执行账本。`events.jsonl` 仅用于观测，不能作为可靠的远程执行队列。
- 公告板只协调修改范围，不替代 Git diff、测试、代码评审和提交记录。

## 工具包文件

| 文件 | 用途 |
|---|---|
| `SKILL.md` | Agent Skill 的触发条件和标准工作流 |
| `agents/openai.yaml` | Codex 的展示和调用元数据 |
| `agent_board.py` | 原本地 CLI 与网络命令入口 |
| `install.py` | 目标项目安装与升级 |
| `schema.json` | 状态、留言和事件协议 |
| `AGENTS.snippet.md` | 注入目标项目的 Agent 规则 |
| `test_agent_board_kit.py` | 生命周期、并发、独立复制和安装幂等测试 |

运行工具包测试：

```powershell
cd agent-board-kit
python -m unittest -v test_agent_board_kit test_board_network test_collaboration test_network_runtime test_coordination
```

## 版本发布

本地协议与网络组件分别标注版本。已发布的归档见 [GitHub Release](https://github.com/Evilom/agent-board-kit/releases)。

## License

[MIT](LICENSE)
