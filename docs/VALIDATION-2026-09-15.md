# 本轮验证记录

## 交付范围

- 工程：`Evilom/agent-board-kit`
- 基线：`Evilom/agent-board-kit`，`4260d637d5d26d9abdc30530cd81b926fee35952`
- 更新分支：`main`，Windows 客户端直接拉取主分支更新。
- 本地 Board 协议 v1，网络组件 0.1.0 预览版；本轮没有发布 GitHub Release。
- 主电脑为服务端＋本机客户端；副电脑为客户端。Windows 实机验证按本轮约定留待设备连接后进行。

## 自动验证

`python3 -m unittest -v test_agent_board_kit.py test_board_network.py`

29 项测试中 28 项通过，1 项因工具包以独立仓库运行而按原测试条件跳过。

覆盖：原 CLI/安装器/worktree 兼容、并发请求只派发一次、提交回执丢失、重建 Hub 后对账、断线期间保留证据、验收权限、离线 Worker 不回退主机、项目和工作区越权、实际 HTTP 鉴权、中文知识更新和删除、索引旧片段、符号链接与路径穿越、Dagu 发布版字段、缺失/错误收据、只读检查前后文件和 Git index 哈希一致、客户端只启动 Worker、远程 TLS 要求、Windows 官方 tar.gz 包格式、配对配置备份与独立 token。

Python 编译检查、`git diff --check`、两个 shell 启动脚本的 `sh -n` 检查通过。

## Mac 真实服务验证

Dagu 二进制：2.16.6，macOS arm64；官方归档 SHA-256：

```text
39e616a65337aeaf6017bc4b24da0c24e5b0bc0b2f2b6bf067ef6c2a138cc927
```

2026-09-15 12:15（Asia/Shanghai）完成真实服务端到 Worker 的验收检查：

| 项目 | 观察结果 |
| --- | --- |
| 任务 | `task-b4576c07483443609ce79be2174ecdac` |
| Dagu 执行 | `attempt-e9795c8a27604158a6a2f32e930684bc` |
| 实际 Worker | `mac-local` |
| 原生 OS | `Darwin` |
| 配方 | `git.inspect/v1` |
| 退出码 | `0` |
| 验收状态 | `accepted`，仅针对 Git 元数据检查 |
| 重复请求 | 返回相同 task/run，账本只有一次 `dispatch.started` |
| 日志 SHA-256 | `537ae9a476eaa9623fdb5a9b5f47a55ae236248ab422f8cb363e1ecea8960119` |

通过 Board 查询了 Mac 已运行的 QMD 网关，限定一份已授权的真实文档，回读原文生成 SHA-256 版本；`ungranted-project` 的请求返回 403。原文和 QMD 索引未修改。具体原文路径与检索结果保留在本地验证记录中。其他项目和整个知识库没有自动开放。

完整停止服务监督进程后，其中心、Hub、Worker 三个子进程全部退出；再次启动后，验收状态、收据、日志哈希与执行次数不变。SQLite `PRAGMA integrity_check` 返回 `ok`。

实测还修正了在线 API 示例与 2.16.6 发布版的差异：发布版使用 `dagRunDetails`；日志可能通过 `lineCount/totalLines` 表示完整性；DAG 名必须短于 40 字符。早期失败的联调记录保留在本地账本，没有清空账本或伪造成功。

## 当前运行与后续实机门槛

- 本地配置：`.runtime/config.json`；服务与执行记录均保存在 `.runtime/`，该目录不进入源码包。
- Board 地址：`http://127.0.0.1:8940`；Dagu API：`http://127.0.0.1:8188`；协调器：`127.0.0.1:51855`。
- 当前只开放主电脑回环地址；没有配置公网入口或开机自动启动。
- Windows 原生 Worker、真实跨机 mTLS、远程断网场景尚未实机验证；本轮完成相应配置/启动脚本和故障模拟测试。
- 没有桌面 GUI、源码快照传输、项目构建执行、补丁回写或 HAPI 会话接入。这些不属于本轮固定只读配方的验收结果。
