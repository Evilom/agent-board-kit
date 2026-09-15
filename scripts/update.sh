#!/bin/sh
# Portable Agent Board: update source on main; preserve local configuration/data.
set -eu
KIT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$KIT_DIR"
CONFIG=${1:-.runtime/config.json}
[ "$(git branch --show-current)" = main ] || { echo '请先切换到 main 分支'; exit 1; }
[ -z "$(git status --porcelain)" ] || { echo '源码存在本地改动，请先保存；更新不会覆盖它们。'; exit 1; }
git pull --ff-only origin main
python3 agent_board.py network --config "$CONFIG" upgrade
python3 -m unittest test_board_network test_collaboration test_network_runtime test_coordination -q
echo '源码和配置升级完成。请启动 service，然后运行 open 打开客户端。'
