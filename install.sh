#!/bin/sh
# macOS / Linux 一步安装入口。
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

for PYTHON in python3 python3.14 python3.13 python3.12 python3.11; do
    if command -v "$PYTHON" >/dev/null 2>&1 && \
       "$PYTHON" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' 2>/dev/null; then
        exec "$PYTHON" "$SCRIPT_DIR/install.py" "$@"
    fi
done

echo "安装失败：请先安装 Python 3.11 或更高版本。" >&2
exit 1
