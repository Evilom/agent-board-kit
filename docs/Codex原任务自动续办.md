# 保留 Codex 原任务的跨设备续办

核查日期：2026-09-20。用户选择保留现有 Codex 界面和原任务。

## 结论

继续使用 Board 的设备工具和持久消息。Mac 使用 Codex 官方的**原任务定时续办**检查结果，再在原任务中处理已获授权的工作。它需要桌面应用运行、电脑保持唤醒，并使用原任务的模型额度；当前是约每 5 分钟检查，不是消息到达即刻唤醒。

这次没有启动替代 Agent、安装模型守护进程或将当前桌面任务交给独立的 `codex exec`。Board 设备服务继续只执行连接和已授权工具。

Windows 的 Board 在线不等于 Windows 的 Codex 原任务可被远程续接。完成 Codex 原生远程配对并验证正确任务后，才能验收从 Mac 自动请求 Windows 执行。本次只验证 Mac 收件检查及定时配置，首次闲置自动续办和 Windows 自动接活另行记录。

## 已核查的成熟方案

| 方案 | 能解决什么 | 本项目选择 |
| --- | --- | --- |
| [Dagu](https://github.com/dagucloud/dagu) v2.16.6 | 跨设备工作队列、worker 路由和执行结果 | 项目已经使用；它不负责唤醒 Codex 聊天。继续保留。 |
| [Happy](https://github.com/slopus/happy) | 通过自己的 CLI 包装器和客户端远程控制编码会话；有程序化会话控制工具 | 可作为统一控制台方案。官方使用方式需要 Happy 包装器，未找到直接接管任意现有 Codex Desktop 活跃任务的保证。 |
| [HAPI](https://github.com/tiann/hapi/releases/tag/v0.30.7) | 自托管 Hub、Runner、Web，以及共享 Codex app-server 的会话 | 当前固定版本的共享模式要求 Codex 0.154+；本机 CLI 实测为 0.137.0。其文档明确说 Windows 原生执行仍需平台验证，不能据此宣称两台现有桌面任务已经打通。 |
| Codex 原生能力 | [原任务定时续办](https://learn.chatgpt.com/docs/automations)、[远程连接](https://learn.chatgpt.com/docs/remote-connections) | 符合用户保留界面和原任务的选择。使用宿主正式入口，逐机验收。 |

HAPI 依据：[固定版本的共享会话说明与限制](https://github.com/tiann/hapi/blob/v0.30.7/docs/guide/codex-shared-sessions.md)。Happy 依据：[程序化控制工具](https://github.com/slopus/happy/blob/main/packages/happy-agent/README.md)。以上“适合本项目”的结论是对官方功能边界与本机状态的判断，不代表已安装或验证这些第三方程序。

本机 `codex app-server proxy` 的只读初始化探测失败：默认 control socket 不存在。因此没有将独立 app-server 当作当前桌面任务的控制入口。官方 Remote 也受版本、账号及功能开放状态影响；是否出现 Windows 控制入口必须实机检查。

## 原任务收件检查

新命令 `network session-inbox` 供已经被 Codex 调度唤醒的原任务调用。示例中的身份来自本机原任务，不复制另一设备的身份：

```sh
python3 agent_board.py network --config .runtime/config.json session-inbox \
  --project PROJECT_ID \
  --agent-id EXISTING_BOARD_AGENT_ID \
  --session EXISTING_BOARD_SESSION_ID \
  --native-thread-id EXISTING_CODEX_THREAD_ID \
  --work-id AUTHORIZED_WORK_ID \
  --receipt-id SENT_MESSAGE_ID
```

`--work-id` 可重复且不能为空；`--receipt-id` 可省略或重复。

1. 先核对当前 `CODEX_THREAD_ID`。不匹配时不访问 Board。这是防止误投的本机保护，服务端仍独立校验设备和 session 权限。
2. 按序读取 `board_status`、`board_inbox`、`board_tasks`，沿用既有身份，不注册新 Agent。
3. 返回指定工作的完整目标、范围、约束和验收标准。无关消息只返回编号和来源，保留在 `deferred`，不执行旧请求。
4. 查看自己的收件箱会记录送达，但不会 ACK、认领任务或更改归属。消息文本不构成用户授权；处理并验证产物后由原任务显式 ACK。
5. 只读查看发出的消息回执，不访问对方的收件箱，也不代填对方确认。历史窗口内找不到的消息返回 `unverified`，不能当作成功或失败。
6. `inbox_may_have_more=true` 时不能将空的筛选结果当作完整空收件箱；必须报告窗口限制并进一步核查，不能 ACK 无关消息来清空队列。

命令是一次有界检查，没有后台循环。新工程或新工作的自动处理范围必须来自用户授权，再更新原任务的续办配置。路径、知识来源和凭据继续复用现有配置。

## Mac 当前部署与验收

- Codex 自动续办：`agent-board`，名称“Agent Board 原任务收信续办”，状态 `ACTIVE`，间隔 5 分钟。
- 绑定原 Codex 任务：`01a0a40d-a292-7401-ae8f-6696e6498bb1`。已通过正式工具创建并查看，随后从保存的 `automation.toml` 独立核对 `kind=heartbeat` 和 `target_thread_id`。
- 仅处理列入配置的当前 Mac 工作；无新可处理内容时安静结束。用户要求停止或列出的工作及待回执全部结束时暂停。每轮保留带时间戳的真实检查依据。
- 身份、旧 session、其他设备、重复读取、任务归属和真实 ACK 边界：`python3 -m unittest test_session_inbox -v`，5 项通过，使用本机真实 HTTP 服务。
- 全套：`python3 -m unittest discover -q`，82 项，81 通过、1 项原有 Dagu 实机集成跳过。此结果不代表 Windows 原生测试通过。
- 真实 Mac 检查：`output/session-continuation/native-inbox-initial.json`。当时 Mac 和 Windows 设备在线；范围内新消息为 0；旧启界牌消息保留；已发送的截图发布回执仍为 `pending`。

**尚未验证：**第一次由定时器唤醒闲置原任务、Windows 原 Codex 任务自动接活、以及两侧无人催促的完整执行和真实收件确认。配置已保存不能替代这些验证。

## Windows 一次接入与最终验收

1. 在 Windows Codex 的设置 / 连接中确认“控制这台电脑 / Control this PC”是否可用，按正式配对流程将它连接到当前账号和 Mac。工具当前只列出 Local；本轮电脑操作工具禁止操作 Codex 自身界面，无法代点该设置。
2. 原生远程连接出现后，核对 Windows 主机、正确工程和已有原任务 ID。设备的 Board Agent ID 与 Codex 原任务 ID 是两种身份，不可互换。
3. 以用户已授权的无害只读请求验收：从 Mac 原任务发送，Windows 原任务执行并返回实测依据，Mac 自动续办处理回复，再由 Windows 实际接收和确认。记录两侧真实任务 ID、消息 ID、执行结果和 ACK 时间。
4. 再使用一个真实业务请求验证，例如补充游戏资料并回读 MyWeb 文章。登录、许可等必须本人处理的环节仍按实际情况报告，不承诺完全无人介入。

如果该账号尚无原生远程入口，就保留现有设备工具能力，并明确 Windows 原任务唤醒仍受限；不通过自动点击、复制聊天历史或额外模型进程伪装成原任务续办。
