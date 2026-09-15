#!/bin/sh
# Start the server role, including its local client. Configuration is kept in one file.
set -eu
KIT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec python3 "$KIT_DIR/agent_board.py" network --config "${1:?Usage: server.sh /path/to/config.json}" service
