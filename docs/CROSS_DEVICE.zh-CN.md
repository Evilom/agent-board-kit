# Agent Board：服务端与客户端

本地 Board 保持 `1.1.1` 的 v1 状态协议；本次增加可选网络组件 `0.1.0` 预览版。主电脑安装**服务端＋客户端**，副电脑安装**客户端**。

```text
主电脑（Mac）
  Board 服务端：项目、权限、任务、收据、验收、知识查询
  Dagu 中心：排队与分配一次执行
  本机客户端：Dagu Worker，参与本机检查
        │ HTTPS（查询和任务）＋ mTLS（执行通道）
副电脑（Windows / Mac / Linux）
  Board CLI：查询知识、发起任务、读取结果
  Dagu Worker：执行固定配方，回传日志
```

`network service` 读取一个配置文件的 `role`：`server` 启动中心、Board 服务端和本机 Worker；`client` 只启动 Worker。CLI 随客户端提供。配置与凭据放在设备本地同一个配置目录中；配置只引用 token 文件，不内嵌 token。原始知识和既有知识服务的密钥留在主电脑。

## 这轮已经实现

- 项目 ID 与设备、环境、工作区映射；Windows 原生与 WSL 应登记为不同环境。
- 本地管理员配对；独立设备 token；按项目授予 `read/query/execute/accept`，按工作区授予执行权限。新配对客户端默认没有验收权限。
- `git.inspect/v1` 固定配方：核对指定提交，读取 Git 状态，返回原生 OS、设备、工作区、时间、退出码和状态摘要。
- Dagu v2.16.6 HTTP 适配、目标 Worker 标签和健康校验、持久任务/执行 ID、请求去重、结果对账。
- `documents` 和既有 `qmd-http` 知识接口。仅返回配置允许的项目与路径，回读原文件生成 SHA-256 版本和片段，删除的文件不再返回。
- 本地 SQLite WAL 执行账本、审计事件、已核验日志与收据持久保存；成功后进入 `review`，由获准人员或 Agent 根据证据执行 `accept`。

**目前的客户端是 CLI＋执行服务，没有桌面图形界面。** 本轮固定配方只检查 Git 元数据，不运行项目代码、构建或测试，不提供任意远程 Shell。未提交改动可被发现并保留，但这里的 Git 状态摘要不是工作区内容快照，也不保证并发编辑被锁住。

## 主电脑安装

需要 Python 3.8+、Git。解压工具包或使用本仓库。

```bash
python3 agent_board.py network --config .runtime/config.json init-server \
  --project my-project --device mac-main --root /absolute/path/to/my-project

python3 agent_board.py network --config .runtime/config.json runtime-install
sh scripts/server.sh .runtime/config.json
```

`runtime-install` 下载固定的 Dagu 2.16.6，并核对官方发布的 SHA-256。已有不同版本的二进制不会被覆盖。网络或 Python 证书链有问题时应修复连接或证书配置，不能关闭 TLS 校验；也可以把经校验的官方二进制放到配置 `runtime_dir` 下的 `bin/dagu`（Windows 为 `bin/dagu.exe`）。

默认 Board 监听 `127.0.0.1:8940`，Dagu API 为 `127.0.0.1:8188`，协调器为 `127.0.0.1:51855`。本地先跑通，再按下一节开放跨设备连接。停止启动脚本会停止它持有的子进程；运行数据保留在 `runtime_dir` 和 `database`，重启读取原记录。

检查连接和任务：

```bash
python3 agent_board.py network --config .runtime/config.json doctor
python3 agent_board.py network --config .runtime/config.json projects
python3 agent_board.py network --config .runtime/config.json query --project my-project "关键词"
python3 agent_board.py network --config .runtime/config.json inspect \
  --project my-project --workspace mac-main --commit <完整提交哈希> --key verify-001
python3 agent_board.py network --config .runtime/config.json reconcile <task_id>
python3 agent_board.py network --config .runtime/config.json show <task_id>
python3 agent_board.py network --config .runtime/config.json accept <task_id> --note "已核对目标版本和检查收据"
```

一个逻辑请求始终使用相同 `--key`；相同 key 配不同输入会被拒绝。查询、验收命令可以在其他终端中运行。`accept` 只表示这一配方的验收，不表示项目已经构建或测试通过。

## 配对 Windows 客户端

### 从 GitHub 获取或更新

本次预览版在 `feat/cross-device-adapters` 分支。首次获取：

```powershell
git clone --branch feat/cross-device-adapters https://github.com/Evilom/agent-board-kit.git
cd agent-board-kit
```

已有工具包仓库时，在仓库目录运行：

```powershell
git fetch origin
git switch feat/cross-device-adapters
git pull --ff-only
```

若本地修改阻止切换或拉取，先保留并处理这些修改，不要使用强制重置。若以前通过安装器把 Board 复制进其他工程，拉取工具包后还需更新对应工程：

```powershell
python install.py D:\your-project --network
```

Git 更新分发源码和启动脚本。设备配对文件、token、证书和原有运行数据保留在本机；首次连接服务端仍需完成下面的配对步骤。

### 1. 配置跨设备通道

推荐通过可信内网或 VPN 连接。应用的项目授权仍独立生效。

Board 直接提供 HTTPS 时，在主配置 `listen` 中设置 `host`、`cert_file` 和 `key_file`，并使客户端 `hub.url` 的主机名与证书匹配；客户端操作系统/Python 需要信任签发 CA。也可由现有 HTTPS 反向代理转发到回环地址。

Dagu 的 `coordinator` 设置可达的 `host/advertise/port`，以及 `peer_insecure: false`、`peer_ca_file`、`peer_cert_file`、`peer_key_file`。本机 `worker` 也设置这组证书，并使用证书覆盖的协调器地址。副电脑使用自己独立签发的客户端证书；不要传输 CA 私钥或主电脑服务器私钥。

仅在明确受信的私有网络验证时，可显式选择 `listen.allow_private_http`、客户端 `hub.allow_private_http` 和 Worker 的 `allow_private_peer`；默认不会开放这条路径。Dagu API 始终只监听主电脑回环地址，不对副电脑开放。

### 2. 主电脑登记设备和工程路径

```bash
python3 agent_board.py network --config .runtime/config.json pair \
  --project my-project --device windows-main --environment windows-native \
  --workspace windows-checkout --os Windows --root 'D:/projects/my-project' \
  --hub-url https://your-mac.example:8940 --coordinator your-mac.example:51855
```

命令备份原配置后增加一台设备，导出 `pairings/windows-main/client.json` 和独立 token 文件。工程路径是 Windows 上已经存在的 Git 仓库根目录，不会从 Mac 自动同步。重新启动服务端以加载新的授权。

### 3. 副电脑只安装客户端

把工具包、这台设备的 `client.json`、token、证书安全地复制到 Windows。将证书放到配置引用的位置，确认 Git 和 Python 命令可用。

```powershell
python agent_board.py network --config .\connection\client.json runtime-install
.\scripts\client.ps1 -Config .\connection\client.json
```

另开终端后可使用同样的 `projects/query/inspect/show/reconcile` 命令。服务端可向 `windows-checkout` 派发任务；调度要求 `board_device=windows-main`、`board_environment=windows-native`、`os=windows`，收据也会再次检查原生系统。WSL 的 Linux Worker 不会冒充 Windows 原生 Worker。

撤销设备：将主配置相应 principal 设置 `disabled: true`，并撤销其 Dagu 客户端证书、停止对应 Worker，再重启服务。只禁用 Board token 不会自动终止已经派发的进程。

## 接入已有知识

在服务端唯一配置的 `knowledge_sources` 中增加来源，示例见 [配置样例](../examples/network/server.example.json)。

- `documents`：读取获准 Markdown/text 文件。适合项目 README、小型 docs 目录；最多枚举 5000 文件。
- `qmd-http`：复用已有网关的 `POST /search`，请求体为 `{"query":"...","n":20}`，响应为 `{"ok":true,"results":[{"file":"qmd://collection/path.md","docid":"#..."}]}`。这是**既有知识网关协议**，不是声称 QMD 原生 MCP HTTP 接口提供 `/search`。
- `root` 是服务端原文目录，`prefixes` 是允许读取的相对路径；`projects` 明确列出可使用这个来源的项目。symlink 和 URL 编码均不能绕过路径范围。
- 返回 `version` 为查询时原文的 SHA-256，`index_version` 为上游索引标识，`verified_at` 为回读时间。片段从原文取得，不转发旧索引摘要。
- 索引还未收录的新文件可能暂时查不到；不会冒充完整实时索引。当前实现是关键词检索，不自动更新索引、不修改原始知识、不生成新的长期记忆。
- 源服务不可用时返回该来源的 `unavailable`，不返回离线缓存伪装成最新内容。文档内容本身不等于当前代码实现或测试证据。

## 断线、重启与验收

| 状态 | 含义和操作 |
| --- | --- |
| `prepared` | 已记录，尚未提交；Worker 不在线时保留这里。恢复后用相同 key 再次请求。 |
| `submitting` | 提交前已落盘。服务端此时崩溃，也不能再次盲目发送。 |
| `queued/running` | Dagu 已确认，执行状态仍需 `reconcile` 查询。 |
| `unknown` | 回执丢失、网络异常、证据缺失或不匹配；只按原执行 ID 对账。 |
| `review` | 目标设备、版本、退出码和完整日志已核验，等待验收。 |
| `accepted` | 有明确审阅记录。 |
| `failed` | 后端确认失败，保留已取得的证据。 |

不后台自动重试执行，不因离线更换设备，不从本地 Board 的 `sweep` 推导远程执行状态。SQLite 仅放在主电脑本地磁盘，不把数据库放进多机共享盘。本地 Board 的 `events.jsonl` 继续用于观测，不作为可靠远程执行队列。

本轮未提供任意源码/未提交改动快照传输、隔离构建、补丁回写、交互式编码会话或桌面 UI。HAPI 留作后续会话适配；OpenClaw、Agent Mail 的代码未复制到本项目。

## 安装到原有项目

```bash
python install.py /absolute/path/to/project --network
python /absolute/path/to/project/scripts/agent_board.py network --config /absolute/path/to/config.json projects
```

不传 `--network` 时安装行为保持原样；旧 `start/update/block/done/release/message` 不需要网络依赖，也不连接 Hub。网络服务不会修改旧 Board 状态。

## 验证与来源

```bash
python3 -m unittest -v test_agent_board_kit.py test_board_network.py
python3 -m py_compile agent_board.py install.py board_network/*.py
```

接口根据 [Dagu 分布式文档](https://docs.dagu.sh/server-admin/distributed/) 和 [REST API 文档](https://docs.dagu.sh/web-ui/api) 实现，并用实际 2.16.6 响应验证（包括 `dagRunDetails` 包装、日志总行数和 DAG 名长度限制）。Dagu 独立运行并由其自身许可证约束；工具包不包含其源码或二进制。知识候选参考 [QMD 官方仓库](https://github.com/tobi/qmd)，优先复用本机已部署网关。
