# Agent Board Kit

一个面向 Codex、Claude Code、Cursor、ZCode 等编码 Agent 的本地协作公告板。它使用 JSON/JSONL 和跨进程文件锁记录任务范围、进度、阻塞、留言及交接，不需要服务端、数据库、Node.js 或第三方 Python 包。

当前版本 `1.1.0` 增加了任务生命周期校验、文件/范围冲突预警、状态备份与修复、结构化状态输出，以及可选的 Git worktree 共享存储。

## 适合什么场景

- 多个编码 Agent 同时使用一个 Git checkout，开工前需要声明任务与文件范围。
- 同一个 clone 下有多个 linked worktree，需要共享任务状态。
- 希望协作记录可本地审计，但不想部署服务、数据库、Web UI 或完整 Agent 编排平台。

它只负责协调，不负责创建 Agent、调度模型或执行任务。需要完整多 Agent 编排、跨机器消息总线或可视化控制台时，可以把它与其他系统组合使用。

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
- 不同电脑、不同 clone 仍然各自独立；需要跨机器协作时，应由外部服务消费 `events.jsonl`，而不是提交运行态文件。
- 公告板只协调修改范围，不替代 Git diff、测试、代码评审和提交记录。

## 工具包文件

| 文件 | 用途 |
|---|---|
| `SKILL.md` | Agent Skill 的触发条件和标准工作流 |
| `agents/openai.yaml` | Codex 的展示和调用元数据 |
| `agent_board.py` | 独立 CLI，唯一功能实现 |
| `install.py` | 目标项目安装与升级 |
| `schema.json` | 状态、留言和事件协议 |
| `AGENTS.snippet.md` | 注入目标项目的 Agent 规则 |
| `test_agent_board_kit.py` | 生命周期、并发、独立复制和安装幂等测试 |

运行工具包测试：

```powershell
cd agent-board-kit
python -m unittest -v test_agent_board_kit.py
```

## License

[MIT](LICENSE)
