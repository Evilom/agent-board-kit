#!/bin/sh
set -eu
KIT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec python3 "$KIT_DIR/agent_board.py" network --config "${1:?请提供本设备配置路径}" open
